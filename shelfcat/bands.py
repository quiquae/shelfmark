"""Automatic shelf-layer (band) detection.

Each frame shows one layer fully and clips its neighbours above and below.
Only the fully visible layer should be transcribed -- the clipped ones get
their own frames, and transcribing them twice would inflate the catalogue
with half-read duplicates.

Book spines produce dense vertical edges; shelf boards, wall and empty wood
do not. Summing vertical-edge energy across each image row therefore gives a
profile whose plateaus are shelf layers and whose valleys are the boards
between them. No training data, no model -- just the physics of what a row of
spines looks like.
"""
import numpy as np
import cv2


def edge_profile(img_bgr, smooth: int = 41):
    """Row-wise vertical-edge energy, normalised to 0..1."""
    g = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    g = cv2.GaussianBlur(g, (5, 5), 0)
    sx = np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3))
    prof = sx.mean(axis=1)
    k = np.ones(smooth) / smooth
    prof = np.convolve(prof, k, mode="same")
    lo, hi = prof.min(), prof.max()
    return (prof - lo) / (hi - lo + 1e-9)


def find_bands(img_bgr, thresh: float = 0.42, min_frac: float = 0.06):
    """Contiguous runs of high vertical-edge energy = candidate shelf layers.
    Returns [(y0, y1, mean_energy, height_fraction)] top to bottom."""
    prof = edge_profile(img_bgr)
    H = len(prof)
    on = prof >= thresh
    bands, start = [], None
    for y in range(H):
        if on[y] and start is None:
            start = y
        elif not on[y] and start is not None:
            if (y - start) / H >= min_frac:
                bands.append((start, y, float(prof[start:y].mean()), (y - start) / H))
            start = None
    if start is not None and (H - start) / H >= min_frac:
        bands.append((start, H, float(prof[start:H].mean()), (H - start) / H))
    return bands


def primary_band(img_bgr, pad_frac: float = 0.012):
    """Pick the layer this frame is actually *of*.

    Scored on three things a fully-captured layer has and a clipped neighbour
    does not: it is tall in frame, its edges are strong (in focus), and it sits
    near the middle rather than running off the top or bottom.

    Returns (y0, y1, diagnostics)."""
    H = img_bgr.shape[0]
    bands = find_bands(img_bgr)
    if not bands:
        return 0, H, {"reason": "no band found; using whole frame", "n_bands": 0}

    scored = []
    for (y0, y1, energy, frac) in bands:
        centre = ((y0 + y1) / 2) / H
        centrality = 1.0 - abs(centre - 0.5) * 2          # 1 at middle, 0 at edge
        touches = (y0 <= 2) or (y1 >= H - 2)              # clipped by the frame
        score = frac * 2.0 + energy * 1.0 + centrality * 1.2 - (0.9 if touches else 0)
        scored.append((score, y0, y1, energy, frac, centrality, touches))
    scored.sort(reverse=True)
    best = scored[0]
    pad = int(H * pad_frac)
    y0, y1 = max(0, best[1] - pad), min(H, best[2] + pad)
    runner = scored[1][0] if len(scored) > 1 else None
    diag = {
        "n_bands": len(bands), "score": round(best[0], 3),
        "energy": round(best[3], 3), "height_frac": round(best[4], 3),
        "centrality": round(best[5], 3), "clipped": bool(best[6]),
        "margin_over_runner_up": round(best[0] - runner, 3) if runner is not None else None,
    }
    # A thin margin means two layers looked equally like the subject.
    diag["confident"] = runner is None or (best[0] - runner) >= 0.35
    return y0, y1, diag


def book_extent(img_bgr, y0: int, y1: int, thresh: float = 0.30, pad_frac: float = 0.01):
    """Horizontal extent of the books within a band.

    Frames routinely include a wall, a shelf end or a run of empty board, and
    on a 1500px viewing width that dead space is stolen directly from the
    resolution available to spine text -- the left 44% of IMG_2152 is wall.
    Trimming to the books is worth roughly a 2x gain in legible text size on
    the frames that need it most.

    Returns (x0, x1)."""
    strip = img_bgr[y0:y1]
    g = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
    g = cv2.GaussianBlur(g, (5, 5), 0)
    sy = np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))   # horizontal edges:
    sx = np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3))   # text and spine gaps
    prof = (sx + sy).mean(axis=0)
    k = np.ones(31) / 31
    prof = np.convolve(prof, k, mode="same")
    prof = (prof - prof.min()) / (prof.max() - prof.min() + 1e-9)
    on = np.where(prof >= thresh)[0]
    if len(on) == 0:
        return 0, img_bgr.shape[1]
    W = img_bgr.shape[1]
    pad = int(W * pad_frac)
    return max(0, int(on[0]) - pad), min(W, int(on[-1]) + pad)
