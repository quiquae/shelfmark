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
