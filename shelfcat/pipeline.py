"""End-to-end run: photographs in, catalogue workbook out.

Stages, each independently re-runnable:
  1. ingest    HEIC -> JPEG, orientation, quality gate, capture order
  2. bands     find the shelf layer each frame is actually OF, and trim to books
  3. crops     render transcription-sized images (the vision step reads these)
  4. stitch    segment layers, merge overlapping frames, corroborate reads
  5. checks    volume sequence, duplicate copies, uncertain layer joins
  6. excel     workbook with the uncertainty carried in the rows

Transcription itself is deliberately a file interface: one JSON per frame in
transcripts/. That keeps the vision step swappable and, more importantly,
makes every catalogue row reproducible from an artefact you can inspect.
"""
import glob
import json
import os
import pathlib

import cv2

from .bands import primary_band, book_extent
from .crops import band_crop
from .ingest import ingest
from .stitch import (Read, segment_and_merge, find_suspect_joins,
                     duplicate_report, sequence_anomalies)
from .location import assign, validate, regroup_rows
from .excel import build


def prepare(src_dir, work_dir, pattern="*.HEIC", parts=2, overlap=0.12):
    work = pathlib.Path(work_dir)
    frames, warnings = ingest(src_dir, work / "jpg", pattern=pattern)
    manifest = []
    for f in frames:
        p = work / "jpg" / f"{f.name}.jpg"
        img = cv2.imread(str(p))
        small = cv2.resize(img, (1400, int(1400 * img.shape[0] / img.shape[1])))
        H, W = small.shape[:2]
        y0, y1, diag = primary_band(small)
        x0, x1 = book_extent(small, y0, y1)
        y0f, y1f, x0f, x1f = y0 / H, y1 / H, x0 / W, x1 / W
        span = x1f - x0f
        crops = []
        for i in range(parts):
            a = i / parts - (overlap / 2 if i else 0)
            b = (i + 1) / parts + (overlap / 2 if i < parts - 1 else 0)
            cx0, cx1 = x0f + span * max(0, a), x0f + span * min(1, b)
            out = work / "crops" / f"{f.name}_{i+1}.jpg"
            band_crop(str(p), out, y0f, y1f, cx0, cx1)
            crops.append(str(out))
        manifest.append({**f.as_dict(), "size": f"{f.width}x{f.height}",
                         "y0f": round(y0f, 4), "y1f": round(y1f, 4),
                         "x0f": round(x0f, 4), "x1f": round(x1f, 4),
                         "band_confident": diag.get("confident"), "crops": crops})
    (work / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest, warnings


def load_transcripts(transcript_dir):
    """One JSON per frame; frames are ordered by filename, which the ingest
    step has already cross-checked against EXIF capture time."""
    out = []
    for p in sorted(glob.glob(os.path.join(transcript_dir, "*.json"))):
        d = json.load(open(p))
        reads = []
        for s in d["spines"]:
            r = Read(image=d["frame"], index=s.get("i", 0), title=s.get("title"),
                     author=s.get("author"), legibility=s.get("legibility", "clear"),
                     script=s.get("script", "latin"), item_type=s.get("item_type", "book"),
                     volume=s.get("volume"), at_edge=s.get("at_edge", False))
            r.detail = s.get("detail")
            r.publisher = s.get("publisher")
            r.note = s.get("note")
            reads.append(r)
        out.append((d["frame"], reads))
    return out


def catalogue(transcript_dir, manifest_path, xlsx_path, project="UATX library",
              rows_per_shelf=3, overrides=None):
    frames = load_transcripts(transcript_dir)
    layers, joins = segment_and_merge(frames)

    # The overlap aligner splits a row whenever consecutive frames share no
    # books -- but that is absence of evidence, not evidence of a row end.
    # Empty board beside the books (a bookcase upright, a shelf end) is the
    # physical signal that a row really did stop. Frames with no overlap AND
    # no side gap are rejoined here.
    meta_raw = json.load(open(manifest_path)) if os.path.exists(manifest_path) else []
    man_by_frame = {m.get("name") or m.get("frame"): m for m in meta_raw}
    row_decisions = []
    if man_by_frame:
        layers, row_decisions = regroup_rows(layers, joins, man_by_frame)

    # carry the descriptive fields the merge does not itself reason about
    for layer in layers:
        for b in layer:
            src = max(b.reads, key=lambda r: (r.legibility == "clear", not r.at_edge))
            b.detail = next((getattr(r, "detail", None) for r in b.reads
                             if getattr(r, "detail", None)), None)
            b.publisher = next((getattr(r, "publisher", None) for r in b.reads
                                if getattr(r, "publisher", None)), None)
            notes = [getattr(r, "note", None) for r in b.reads if getattr(r, "note", None)]
            b.note = "; ".join(dict.fromkeys(notes)) if notes else None

    assignments = assign(layers, rows_per_shelf=rows_per_shelf, overrides=overrides)
    loc_warnings = validate(assignments, layers, joins=joins,
                            rows_per_shelf=rows_per_shelf)

    anomalies = [(i + 1, sequence_anomalies(l)) for i, l in enumerate(layers)]
    anomalies = [(i, a) for i, a in anomalies if a]
    dups = duplicate_report(layers)
    suspects = find_suspect_joins(layers)
    meta = meta_raw
    stats = build(xlsx_path, layers, meta, anomalies, dups, suspects, joins, project,
                  assignments=assignments, location_warnings=loc_warnings,
                  row_decisions=row_decisions)
    # `layers` is returned so a caller can build records from the merged
    # books without re-running the merge. Additive only -- the workbook is
    # written exactly as before.
    return {**stats, "layers": layers, "joins": joins, "anomalies": anomalies,
            "duplicates": dups, "suspect_joins": suspects,
            "assignments": assignments, "location_warnings": loc_warnings,
            "row_decisions": row_decisions}
