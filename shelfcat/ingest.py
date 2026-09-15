"""Ingest: HEIC decode, orientation, quality gate, deterministic ordering.

iPhone HEIC carries orientation in EXIF rather than in the pixel data, so a
naive decode yields sideways shelves and unreadable spines. Everything here
goes through ImageOps.exif_transpose first.

Ordering matters more than usual for this pipeline: the overlap stitcher in
stitch.py assumes frames arrive in capture order, left to right along a shelf
layer. Filename sequence and EXIF timestamp are cross-checked, and any
disagreement is reported rather than silently resolved.
"""
import hashlib
import pathlib
import re
from dataclasses import dataclass, asdict

import cv2
import numpy as np
from PIL import Image, ImageOps
import pillow_heif

pillow_heif.register_heif_opener()

# Below this Laplacian variance a whole frame is too soft to transcribe.
# Unlike the barcode case (where downscaling defeats the metric) this is a
# valid gate: these are full-resolution frames at a fixed size.
BLUR_FLOOR = 45.0
MIN_LONG_EDGE = 1600


@dataclass
class Frame:
    name: str
    source: str
    sha256: str
    width: int
    height: int
    captured_at: str | None
    seq: int
    sharpness: float
    quality: str
    quality_note: str

    def as_dict(self):
        return asdict(self)


def _sha(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _seq_key(name: str):
    """IMG_2160.HEIC -> (2160, 0). Handles the 'IMG_1737 2.HEIC' duplicate
    form Finder produces, which must sort after its original."""
    m = re.search(r"(\d+)", pathlib.Path(name).stem)
    n = int(m.group(1)) if m else 0
    dup = re.search(r"[ _-](\d+)$", pathlib.Path(name).stem)
    return (n, int(dup.group(1)) if dup else 0)


def load(path) -> Image.Image:
    """Decode and apply EXIF orientation. Without the transpose, iPhone frames
    come out rotated and every spine is unreadable."""
    return ImageOps.exif_transpose(Image.open(path)).convert("RGB")


def sharpness(img: Image.Image) -> float:
    a = np.asarray(img.convert("L"))
    if max(a.shape) > 1400:                       # normalise scale first, or
        s = 1400 / max(a.shape)                   # the metric tracks resolution
        a = cv2.resize(a, (int(a.shape[1] * s), int(a.shape[0] * s)))
    return float(cv2.Laplacian(a, cv2.CV_64F).var())


def ingest(src_dir, out_dir, pattern="*.HEIC", jpeg_quality=92, max_edge=None):
    src, out = pathlib.Path(src_dir), pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = sorted([p for p in src.glob(pattern)] +
                   [p for p in src.glob(pattern.lower())], key=lambda p: _seq_key(p.name))
    files = list(dict.fromkeys(files))

    frames, times = [], []
    for i, p in enumerate(files):
        img = load(p)
        exif = img.getexif()
        cap = exif.get(306) or exif.get(36867)
        if max_edge and max(img.size) > max_edge:
            s = max_edge / max(img.size)
            img = img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS)
        sh = sharpness(img)
        if max(img.size) < MIN_LONG_EDGE:
            q, note = "low_res", f"long edge {max(img.size)}px < {MIN_LONG_EDGE}"
        elif sh < BLUR_FLOOR:
            q, note = "soft", f"sharpness {sh:.0f} < {BLUR_FLOOR:.0f} - reshoot this frame"
        else:
            q, note = "ok", ""
        dest = out / (p.stem + ".jpg")
        img.save(dest, "JPEG", quality=jpeg_quality, optimize=True)
        frames.append(Frame(name=p.stem, source=str(p), sha256=_sha(p),
                            width=img.width, height=img.height, captured_at=cap,
                            seq=i, sharpness=round(sh, 1), quality=q, quality_note=note))
        times.append(cap)

    warnings = []
    known = [(f.seq, t) for f, t in zip(frames, times) if t]
    if len(known) > 1 and any(known[i][1] > known[i + 1][1] for i in range(len(known) - 1)):
        warnings.append("EXIF capture times are not monotonic in filename order; "
                        "left-to-right assumption may be wrong for some frames")
    return frames, warnings
