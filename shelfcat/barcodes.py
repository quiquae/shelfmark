"""Barcode evidence extraction.

Design notes grounded in measurement (see tests/scale_threshold.py, run
2026-09-01 on synthetic EAN-13):

  * ZBar decoded down to ~2.1 px per module; ZXing needed ~3.2 px/module.
    EAN-13 is 95 modules wide, so the practical floor is ~200 px of barcode
    width for ZBar and ~300 px for ZXing.
  * Variance-of-Laplacian ROSE as barcodes were downscaled past the decode
    limit, so it is NOT a usable gate for the resolution failure mode.
    Gate on measured barcode width in pixels instead.
  * Both decoders survived 90-degree rotation, 15-degree skew, low contrast
    and synthetic glare. Only extreme blur (Gaussian k=21) defeated both.

Absolute read rates here are an upper bound: these were rendered barcodes,
not photographs of real covers. Published benchmarks on photo datasets put
ZBar near 90% on in-focus and near 14% on out-of-focus EAN-13.
"""
import json
from dataclasses import dataclass, asdict

import cv2
import numpy as np
from pyzbar.pyzbar import decode as _zbar_decode
import zxingcpp

EAN13_MODULES = 95
MIN_WIDTH_SAFE = 300      # both decoders comfortable
MIN_WIDTH_MARGINAL = 200  # zbar only; flag for verification


@dataclass
class BarcodeHit:
    value: str
    detectors: list      # which decoders agreed
    bbox: list           # [x, y, w, h]
    px_width: int
    px_per_module: float
    quality: str         # ok | marginal | low_res
    is_bookland: bool
    isbn13: str | None


def _check_digit_ok(ean: str) -> bool:
    if len(ean) != 13 or not ean.isdigit():
        return False
    s = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(ean[:12]))
    return (10 - s % 10) % 10 == int(ean[12])


def is_bookland(value: str) -> bool:
    """Bookland EAN: an EAN-13 in the 978/979 prefix range encodes an ISBN-13."""
    return value[:3] in ("978", "979") and _check_digit_ok(value)


def _zbar(img):
    out = []
    try:
        for r in _zbar_decode(img):
            x, y, w, h = r.rect
            out.append((r.data.decode("utf-8", "replace"), [x, y, w, h]))
    except Exception:
        pass
    return out


def _zxing(img):
    out = []
    try:
        for r in zxingcpp.read_barcodes(img):
            p = r.position
            xs = [p.top_left.x, p.top_right.x, p.bottom_right.x, p.bottom_left.x]
            ys = [p.top_left.y, p.top_right.y, p.bottom_right.y, p.bottom_left.y]
            out.append((r.text, [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]))
    except Exception:
        pass
    return out


def extract(img) -> list[BarcodeHit]:
    """Union of both decoders. Agreement between them is a useful signal:
    a value only one decoder saw is worth flagging even if it looks valid."""
    found = {}
    for name, fn in (("zbar", _zbar), ("zxing", _zxing)):
        for value, bbox in fn(img):
            if value not in found:
                found[value] = {"detectors": [], "bbox": bbox}
            found[value]["detectors"].append(name)
            # prefer the larger bbox estimate
            if bbox[2] > found[value]["bbox"][2]:
                found[value]["bbox"] = bbox

    hits = []
    for value, meta in found.items():
        # EAN-5 / EAN-2 add-ons are price data, not identity. Drop them.
        if len(value) in (2, 5) and value.isdigit():
            continue
        w = int(meta["bbox"][2])
        ppm = w / EAN13_MODULES if w else 0.0
        if w >= MIN_WIDTH_SAFE:
            q = "ok"
        elif w >= MIN_WIDTH_MARGINAL:
            q = "marginal"
        else:
            q = "low_res"
        bl = is_bookland(value)
        hits.append(BarcodeHit(
            value=value, detectors=sorted(meta["detectors"]), bbox=meta["bbox"],
            px_width=w, px_per_module=round(ppm, 2), quality=q,
            is_bookland=bl, isbn13=value if bl else None))
    return sorted(hits, key=lambda h: (h.bbox[1], h.bbox[0]))


def capture_advice(img_width_px: int, frame_width_mm: float) -> dict:
    """Turn the px/module floor into a capture rule the photographer can use.
    A retail book barcode is about 30 mm wide at typical 80% magnification."""
    px_per_mm = img_width_px / frame_width_mm
    barcode_px = px_per_mm * 30.0
    return {
        "px_per_mm": round(px_per_mm, 2),
        "expected_barcode_px": int(barcode_px),
        "verdict": "ok" if barcode_px >= MIN_WIDTH_SAFE
                   else "marginal" if barcode_px >= MIN_WIDTH_MARGINAL else "too wide a frame",
        "max_frame_width_mm_for_safe": int(img_width_px / (MIN_WIDTH_SAFE / 30.0)),
    }
