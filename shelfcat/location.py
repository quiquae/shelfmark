"""Physical location: Shelf / Row / Position.

The capture protocol is: for each bookcase (Shelf 1, 2, 3 ...), photograph the
top row left to right, then the middle row, then the bottom row, with the
frames overlapping within each row.

So the ordered layers coming out of stitch.segment_and_merge map onto
locations by simple division -- layer 0 is Shelf 1 top, layer 3 is Shelf 2 top.
That mapping is only as good as the layer segmentation underneath it, and a
single missed or spurious boundary shifts every subsequent shelf number. It is
therefore validated rather than trusted, and every check that fails is reported.

Capture timing was evaluated as an independent corroborating signal and
REJECTED: across the 79 frames of 1 Sep 2026 the photographer shot almost
continuously (median gap 3 s), with only one pause over 60 s and two over 12 s.
Row and shelf changes are simply not visible in the timestamps, so nothing here
depends on them. The one long pause is reported as a weak hint, not used.
"""
ROW_NAMES = ("top", "middle", "bottom")


def assign(layers, rows_per_shelf: int = 3, overrides: dict | None = None):
    """Map ordered layers onto (shelf, row).

    overrides: {layer_index: (shelf:int, row:str)} to correct a bad boundary by
    hand without re-running the transcription."""
    overrides = overrides or {}
    out = []
    for i, layer in enumerate(layers):
        if i in overrides:
            shelf, row = overrides[i]
            src = "manual"
        else:
            shelf = i // rows_per_shelf + 1
            row = ROW_NAMES[i % rows_per_shelf] if rows_per_shelf == 3 else f"row {i % rows_per_shelf + 1}"
            src = "derived"
        out.append({"layer": i, "shelf": shelf, "row": row, "source": src,
                    "n_books": len(layer)})
    return out


def validate(assignments, layers, joins=None, long_pause_after=None,
             rows_per_shelf: int = 3, thin_layer: int = 3):
    """Everything that could make the shelf/row numbering wrong.

    These are warnings about the LOCATION MODEL, distinct from the per-book
    uncertainty flags -- a book can be read perfectly and still be filed under
    the wrong shelf if a boundary was missed."""
    warns = []
    n = len(assignments)

    if joins:
        boundaries = sum(1 for j in joins if j["verdict"] == "layer_boundary")
        overlapped = sum(1 for j in joins if j["verdict"].startswith("same_layer"))
        if boundaries > overlapped:
            warns.append({
                "kind": "frames_mostly_do_not_overlap",
                "detail": f"Only {overlapped} of {boundaries + overlapped} consecutive "
                          f"frame pairs share any books. The method assumed 2+ "
                          f"overlapping photographs per shelf row; that mostly did not "
                          f"happen. Without overlap there is NO evidence distinguishing "
                          f"'next photo, same row' from 'next photo, new row', so Shelf "
                          f"and Row are a mechanical guess from photo order, NOT a "
                          f"measurement. Position within a group, and every title and "
                          f"author, are unaffected. Correct groupings via the Frames sheet.",
                "severity": "high"})

    if n % rows_per_shelf:
        warns.append({
            "kind": "layer_count_not_divisible",
            "detail": f"{n} layers detected, which is not a multiple of "
                      f"{rows_per_shelf}. Either a row boundary was missed, a "
                      f"row was split in two, or some bookcase does not have "
                      f"{rows_per_shelf} rows. Shelf numbers after the first "
                      f"error will be wrong.",
            "severity": "high"})

    for a in assignments:
        if a["n_books"] <= thin_layer:
            warns.append({
                "kind": "thin_layer",
                "detail": f"Shelf {a['shelf']} {a['row']} has only "
                          f"{a['n_books']} book(s) — likely a row split in two "
                          f"by a missed overlap, rather than a real short row.",
                "severity": "high", "layer": a["layer"]})

    if joins:
        for j in joins:
            if j.get("verdict") == "same_layer_weak_join":
                warns.append({
                    "kind": "weak_join",
                    "detail": f"{j['left']} → {j['right']} were joined on a "
                              f"single shared book ({j['similarity']}% match). "
                              f"If that match is wrong these are two different "
                              f"rows.",
                    "severity": "medium"})

    if long_pause_after:
        boundary_frames = set()
        if joins:
            boundary_frames = {j["left"] for j in joins if j["verdict"] == "layer_boundary"}
        for frame, secs in long_pause_after.items():
            if frame not in boundary_frames:
                warns.append({
                    "kind": "pause_mid_row",
                    "detail": f"A {secs:.0f}s pause after {frame} did not "
                              f"coincide with a detected row boundary. Weak "
                              f"hint only — check whether a row change was missed.",
                    "severity": "low"})
    return warns


def label(a):
    return f"Shelf {a['shelf']} · {a['row']}"


# A bookcase upright, shelf end or wall leaves empty board beside the books.
# Measured on the 79 frames of 1 Sep 2026: of 29 frames whose books stop >=8%
# of frame width short of the right edge, 28 are followed by a genuine row
# boundary, and only one contradicts an overlap-based join. The signal is
# therefore highly precise, though it catches only about 60% of boundaries on
# its own -- which is exactly what makes it useful as CORROBORATION.
SIDE_GAP = 0.08


def gap_evidence(manifest_by_frame, left_frame, right_frame):
    """Is there physical evidence of a row ending between these two frames?

    Returns (has_evidence, detail). Evidence is empty board at the right edge
    of the left frame (the row ran out) or at the left edge of the right frame
    (a new row started against an upright)."""
    L = manifest_by_frame.get(left_frame, {})
    R = manifest_by_frame.get(right_frame, {})
    right_gap = 1.0 - L.get("x1f", 1.0)
    left_gap = R.get("x0f", 0.0)
    bits = []
    if right_gap >= SIDE_GAP:
        bits.append(f"{right_gap:.0%} empty board right of {left_frame}")
    if left_gap >= SIDE_GAP:
        bits.append(f"{left_gap:.0%} empty board left of {right_frame}")
    return bool(bits), "; ".join(bits)


def regroup_rows(layers, joins, manifest_by_frame):
    """Merge layers the overlap aligner split for lack of evidence.

    The aligner can only see shared books. When consecutive frames of the SAME
    row happen not to overlap, it has nothing to go on and splits the row. The
    side-gap signal supplies the missing evidence: a boundary with no empty
    board on either side is very likely not a real row end, just a gap in the
    photography.

    Returns (rows, decisions). Books are never reordered or dropped; only the
    grouping changes. A merge across a non-overlapping join means the position
    ordering at that seam is ASSUMED from photo order rather than verified,
    and each such merge is recorded so the workbook can say so."""
    boundary_after = {}
    for j in joins:
        if j["verdict"] == "layer_boundary":
            boundary_after[j["left"]] = j["right"]

    rows, decisions = [], []
    cur = list(layers[0]) if layers else []
    cur_frames = _frames_of(layers[0]) if layers else []
    for i in range(1, len(layers)):
        prev_last = _frames_of(layers[i - 1])[-1] if _frames_of(layers[i - 1]) else None
        nxt_first = _frames_of(layers[i])[0] if _frames_of(layers[i]) else None
        has_gap, detail = gap_evidence(manifest_by_frame, prev_last, nxt_first)
        if has_gap:
            decisions.append({"between": f"{prev_last} -> {nxt_first}",
                              "action": "kept as separate rows",
                              "evidence": detail, "verified": True})
            rows.append(cur)
            cur = list(layers[i])
        else:
            decisions.append({"between": f"{prev_last} -> {nxt_first}",
                              "action": "merged into one row",
                              "evidence": "no empty board on either side and no shared "
                                          "books; treated as the same row photographed "
                                          "without overlap",
                              "verified": False})
            cur = cur + list(layers[i])
    if cur:
        rows.append(cur)
    return rows, decisions


def _frames_of(layer):
    seen = []
    for b in layer:
        for s in b.sources:
            if s not in seen:
                seen.append(s)
    return seen
