"""Spine reading.

The single most important rule in this file: the model is asked what is
VISIBLE, never what the book IS. A spine carries title, author and sometimes
publisher. It does not carry edition, printing year or ISBN -- a model asked
for those will invent plausible, wrong ones, and a 1954 Chaucer and a 2008
Chaucer have near-identical spines.

So the output schema has no year and no ISBN field. Those can only enter the
catalogue from an authority record matched on the visible text, or from a
decoded barcode.
"""

SPINE_SCHEMA = {
    "type": "object",
    "properties": {
        "spines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "position":     {"type": "integer", "description": "1-based, left to right"},
                    "bbox":         {"type": "array", "items": {"type": "number"},
                                     "minItems": 4, "maxItems": 4},
                    "title_text":   {"type": ["string", "null"], "description": "verbatim, as printed"},
                    "author_text":  {"type": ["string", "null"], "description": "verbatim, as printed"},
                    "publisher_text": {"type": ["string", "null"]},
                    "volume_text":  {"type": ["string", "null"], "description": "e.g. 'VOL. III'"},
                    "script":       {"type": "string", "enum": ["latin", "greek", "cyrillic", "other", "none"]},
                    "orientation":  {"type": "string", "enum": ["vertical", "horizontal", "flat_stacked"]},
                    "legibility":   {"type": "string", "enum": ["clear", "partial", "illegible"]},
                    "item_type":    {"type": "string",
                                     "enum": ["book", "bound_periodical", "box_or_case", "pamphlet", "unknown"]},
                    "notes":        {"type": ["string", "null"]},
                },
                "required": ["position", "bbox", "title_text", "author_text",
                             "script", "orientation", "legibility", "item_type"],
            },
        }
    },
    "required": ["spines"],
}

PROMPT = """\
You are transcribing a photograph of one shelf of books for a library inventory.

Report ONLY what is physically visible in the image. This is a transcription
task, not an identification task.

Rules, in order of importance:

1. Never infer. If you recognise a work but cannot read its title on this
   spine, `title_text` is null and `legibility` is "illegible". Do not supply
   a title from memory.
2. Transcribe verbatim, including abbreviations, ampersands, capitalisation
   and any misspelling. Do not normalise, expand or translate.
3. Emit one entry for EVERY spine you can see, left to right, including the
   ones you cannot read. A shelf of 31 books must produce 31 entries. Books
   stacked flat count individually; set orientation to "flat_stacked".
4. Near-identical adjacent spines are usually a multi-volume set, not a
   duplicate detection error. Emit each one separately and put whatever
   volume marking you can read in `volume_text`.
5. Never output a year, a date or an ISBN. Those fields do not exist in this
   schema on purpose; they cannot be read reliably from a spine.
6. If part of a spine is hidden behind another object, transcribe what is
   visible and set `legibility` to "partial".

Return JSON matching the provided schema and nothing else.
"""

def count_check(vlm_spines, geometric_count):
    """The model's list length is never trusted on its own. Compare it with an
    independent geometric count of spine regions; disagreement is a review
    flag for the whole shelf, because it means positions may be shifted."""
    n = len(vlm_spines)
    if geometric_count is None:
        return {"ok": None, "reason": "no geometric count available"}
    delta = n - geometric_count
    return {
        "ok": delta == 0,
        "vlm_count": n,
        "geometric_count": geometric_count,
        "delta": delta,
        "action": "accept" if delta == 0 else "flag_shelf_for_recount",
    }


# ---------------------------------------------------------------------------
# The vision seam.
#
# Transcription is a file interface by design: one JSON per frame, which keeps
# the model swappable and makes every catalogue row reproducible from an
# artefact you can open. read_spine() is the callable form of that seam.
#
# NOTE ON SHAPE. SPINE_SCHEMA above uses {position, bbox, title_text, ...}.
# The 79 real transcripts of 1 Sep 2026 use {i, title, author, volume, detail,
# publisher, legibility, at_edge, note} and carry NO bbox. pipeline.
# load_transcripts() reads the latter, so that is what read_spine returns and
# what the fake emits. The consequence is that there are no per-spine crop
# coordinates anywhere in the system: a review screen can show the band crop
# and an ordinal ("spine 3 of 14"), not an individual spine image. Emitting
# `bbox` from the real implementation is what unlocks per-spine crops.
#
# There is no real implementation here on purpose. Supply one; do not let a
# stub quietly become the product.
# ---------------------------------------------------------------------------
import os
import pathlib

FAKE_VISION_ENV = "SHELFCAT_FAKE_VISION"

# Three spines, deliberately including one that cannot be read, so that the
# "nothing is dropped" path is exercised on every fake run rather than only
# when real OCR happens to fail.
_FAKE_SPINES = [
    {"i": 1, "title": "The Collected Letters of Thomas and Jane Welsh Carlyle",
     "author": "Ryals & Fielding", "volume": "Volume 23",
     "detail": "April 1848 - March 1849", "publisher": "Duke",
     "legibility": "clear", "at_edge": True, "note": None},
    {"i": 2, "title": "The Canterbury Tales", "author": "Chaucer",
     "volume": None, "detail": None, "publisher": None,
     "legibility": "clear", "at_edge": False, "note": None},
    {"i": 3, "title": None, "author": None, "volume": None, "detail": None,
     "publisher": None, "legibility": "illegible", "at_edge": False,
     "note": "spine dark, no text recoverable"},
]


def fake_vision_enabled() -> bool:
    return os.environ.get(FAKE_VISION_ENV, "").lower() in ("1", "true", "yes", "on")


def read_spine(crop_path, *, fake: bool | None = None) -> list[dict]:
    """Transcribe one crop into a list of spine reads, left to right.

    Returns the transcript shape (see the note above), one entry per spine
    INCLUDING the illegible ones -- an omitted spine shifts every position
    after it, which corrupts the shelf order silently.
    """
    if fake if fake is not None else fake_vision_enabled():
        return [dict(s) for s in _FAKE_SPINES]
    raise NotImplementedError(
        "read_spine has no real implementation. Supply a vision backend that "
        f"returns the transcript shape, or set {FAKE_VISION_ENV}=1 to run the "
        "pipeline end to end with three hardcoded spines."
    )


def _as_read(frame, i, spine):
    """A transcript dict as the Read that stitch.py's aligner expects."""
    from .stitch import Read
    return Read(image=frame, index=i, title=spine.get("title"),
                author=spine.get("author"),
                legibility=spine.get("legibility", "clear"),
                script=spine.get("script", "latin"),
                item_type=spine.get("item_type", "book"),
                volume=spine.get("volume"), at_edge=bool(spine.get("at_edge")))


def _prefer(a: dict, b: dict) -> dict:
    """Which of two reads of the same spine to keep.

    Mirrors the preference order in stitch._merge_reads -- better legibility,
    then not cut off at an edge, then more text -- so a spine split by a crop
    boundary keeps its uncut read. That is the entire reason crops overlap."""
    from .stitch import LEGIBILITY_RANK

    def key(s):
        return (LEGIBILITY_RANK.get(s.get("legibility"), 0),
                not s.get("at_edge"), len(s.get("title") or ""))

    return a if key(a) >= key(b) else b


def transcribe_frame(frame_name, crop_paths, *, fake=None) -> dict:
    """Assemble one frame's transcript from its overlapping crops.

    crops.split_band cuts a band into slices that overlap on purpose, so a
    spine near a slice boundary is transcribed twice. stitch.py dedupes
    ACROSS frames, never within one -- so the crops must be rejoined here, or
    every boundary spine becomes a phantom second copy in the catalogue.

    The join reuses stitch.best_overlap, the same alignment calibrated against
    real pairs, rather than introducing a second and divergent matching rule.
    A zero-length overlap between two crops of one frame is recorded rather
    than ignored: the crops are known to overlap geometrically, so failing to
    find it means the reads are unreliable across that seam.
    """
    from .stitch import best_overlap

    merged: list[dict] = []
    seams: list[dict] = []
    for path in crop_paths:
        got = [dict(s, crop=str(path)) for s in read_spine(path, fake=fake)]
        if merged and got:
            left = [_as_read(frame_name, i, s) for i, s in enumerate(merged)]
            right = [_as_read(frame_name, i, s) for i, s in enumerate(got)]
            k, sim = best_overlap(left, right)
            for off in range(k):
                at = len(merged) - k + off
                merged[at] = _prefer(merged[at], got[off])
            got = got[k:]
            seams.append({"crop": pathlib.Path(path).name, "overlap": k,
                          "similarity": round(sim, 1)})
        merged.extend(got)

    for i, spine in enumerate(merged, 1):
        spine["i"] = i
    return {"frame": frame_name, "spines": merged, "crop_seams": seams}
