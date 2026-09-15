"""Render transcription-ready crops.

The viewer downsamples anything wider than roughly 1550px, so rendering a
whole 5712px frame throws away exactly the detail spine text needs. Crops are
therefore cut at native resolution and only then scaled to the viewing width,
and a band can be split into overlapping halves when its text is small.
"""
import pathlib
import cv2
from PIL import Image

VIEW_W = 1500


def band_crop(jpg_path, out_path, y0f=None, y1f=None, x0f=0.0, x1f=1.0, view_w=VIEW_W):
    """Crop by fractional coordinates, then scale once to the viewing width."""
    im = Image.open(jpg_path)
    W, H = im.size
    y0 = int((y0f or 0) * H); y1 = int((y1f or 1) * H)
    x0 = int(x0f * W);        x1 = int(x1f * W)
    crop = im.crop((x0, y0, x1, y1))
    if crop.width > view_w:
        crop = crop.resize((view_w, max(1, int(view_w * crop.height / crop.width))), Image.LANCZOS)
    pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    crop.save(out_path, "JPEG", quality=93, optimize=True)
    return crop.size


def split_band(jpg_path, out_dir, stem, y0f, y1f, parts=2, overlap=0.10, view_w=VIEW_W):
    """Cut the band into `parts` overlapping horizontal slices.

    The overlap is not redundancy for its own sake: a spine sitting on a slice
    boundary is cut in half in one slice and whole in the other, and the
    merge step in stitch.py prefers the uncut read."""
    out = []
    span = 1.0 / parts
    for i in range(parts):
        x0 = max(0.0, i * span - (overlap / 2 if i else 0))
        x1 = min(1.0, (i + 1) * span + (overlap / 2 if i < parts - 1 else 0))
        p = pathlib.Path(out_dir) / f"{stem}_p{i+1}.jpg"
        size = band_crop(jpg_path, p, y0f, y1f, x0, x1, view_w)
        out.append({"path": str(p), "part": i + 1, "x0": round(x0, 3), "x1": round(x1, 3), "size": size})
    return out
