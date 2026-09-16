"""The vision backend: a crop of a shelf in, transcribed spines out.

This is the implementation `read_spine` has been declaring a contract for.
Two rules from spines.py govern it and are not negotiable here:

**Transcription, not identification.** The model is asked what is VISIBLE.
SPINE_SCHEMA has no year field and no ISBN field on purpose; a model asked for
them invents plausible, wrong ones, and a 1954 Chaucer and a 2008 Chaucer have
near-identical spines. Those fields can only enter the catalogue from an
authority record matched on the visible text, or from a decoded barcode.

**Every spine gets an entry, including the unreadable ones.** An omitted spine
shifts every position after it and silently corrupts the shelf order, which is
the one thing this software exists to get right.

The prompt and the schema are the ones already written in spines.py. This
module adds the API call, the mapping to the transcript shape, and nothing
else -- no second prompt, no second schema.

Requires an API key: set ANTHROPIC_API_KEY, or run `ant auth login` and the
SDK picks the profile up with no env var.
"""
import base64
import copy
import json
import mimetypes
import os
import pathlib

from .spines import PROMPT, SPINE_SCHEMA, count_check

# Opus 5. Reading thirty spines off one photograph -- rotated text, varying
# type sizes, gilt on dark cloth -- is the hard part of this pipeline, and a
# misread here propagates through resolution into the catalogue. A cheaper
# model is a false economy at roughly a penny a shelf.
DEFAULT_MODEL = "claude-opus-5"
MAX_TOKENS = 16000

# Anything wider than this the viewer downsamples anyway, and crops.py has
# already scaled to VIEW_W. Guard against someone handing us a 24MP frame.
MAX_BYTES = 5 * 1024 * 1024


class VisionError(RuntimeError):
    """The crop could not be transcribed.

    Raised rather than returning an empty list: an empty list is
    indistinguishable from "this crop had no books on it", which would drop a
    whole shelf out of the catalogue without anyone noticing."""


def _client(client=None):
    if client is not None:
        return client
    try:
        import anthropic
    except ImportError as e:                       # pragma: no cover
        raise VisionError(
            "the anthropic SDK is not installed. pip install -e '.[vision]', "
            "or set SHELFCAT_FAKE_VISION=1 to run the pipeline with "
            "hardcoded spines.") from e
    # Zero-arg construction resolves ANTHROPIC_API_KEY, then
    # ANTHROPIC_AUTH_TOKEN, then an `ant auth login` profile. An unset
    # ANTHROPIC_API_KEY does not mean there are no credentials.
    return anthropic.Anthropic()


def _image_block(path):
    p = pathlib.Path(path)
    data = p.read_bytes()
    if len(data) > MAX_BYTES:
        raise VisionError(f"{p.name} is {len(data) // 1024}KB; crops.py should "
                          f"have scaled it to under {MAX_BYTES // 1024}KB")
    media = mimetypes.guess_type(p.name)[0] or "image/jpeg"
    if media not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        raise VisionError(f"{p.name}: {media} is not an image type the API accepts")
    return {"type": "image", "source": {
        "type": "base64", "media_type": media,
        "data": base64.standard_b64encode(data).decode("ascii")}}


def _schema():
    """SPINE_SCHEMA with the strictness structured outputs wants, without
    mutating the module-level schema other code reads."""
    s = copy.deepcopy(SPINE_SCHEMA)
    s["additionalProperties"] = False
    s["properties"]["spines"]["items"]["additionalProperties"] = False
    return s


def _as_transcript(spine, index):
    """One schema entry as the transcript shape the pipeline consumes.

    SPINE_SCHEMA speaks in `title_text`/`author_text`/`position`;
    pipeline.load_transcripts reads `title`/`author`/`i`. The bbox is carried
    through unchanged -- it is the field that lets the review screen crop an
    exact spine instead of estimating one from an ordinal.

    A spine the model marked illegible keeps its entry with a null title. It
    is a volume on a shelf whether or not anyone could read it."""
    leg = spine.get("legibility") or "clear"
    title = (spine.get("title_text") or "").strip() or None
    return {
        "i": index,
        "title": None if leg == "illegible" else title,
        "author": (spine.get("author_text") or "").strip() or None,
        "volume": (spine.get("volume_text") or "").strip() or None,
        "publisher": (spine.get("publisher_text") or "").strip() or None,
        "detail": None,          # never inferred; only an authority supplies it
        "legibility": leg,
        "script": spine.get("script") or "latin",
        "item_type": spine.get("item_type") or "book",
        "orientation": spine.get("orientation"),
        "bbox": spine.get("bbox"),
        "note": (spine.get("notes") or "").strip() or None,
        "at_edge": False,        # set by the caller, which knows the crop edges
    }


def transcribe(crop_path, *, model=None, client=None, geometric_count=None):
    """Transcribe one crop. Returns spines in transcript shape, left to right.

    `geometric_count`, when an independent count of spine regions is
    available, is checked against the model's list length: a disagreement
    means positions may be shifted, which is a review flag for the whole
    shelf rather than for one book."""
    # The client is built first: _client turns a missing SDK into a
    # VisionError with an actionable message, where a bare ImportError from
    # the line below would just say "No module named 'anthropic'".
    cl = _client(client)
    import anthropic

    try:
        resp = cl.messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": _schema()}},
            messages=[{"role": "user", "content": [
                _image_block(crop_path),
                {"type": "text", "text": PROMPT},
            ]}],
        )
    except anthropic.NotFoundError as e:
        raise VisionError(f"model not available: {e}") from e
    except anthropic.AuthenticationError as e:
        raise VisionError("no valid credentials. Set ANTHROPIC_API_KEY or run "
                          "`ant auth login`.") from e
    except anthropic.RateLimitError as e:
        raise VisionError(f"rate limited: retry later ({e})") from e
    except anthropic.APIStatusError as e:
        raise VisionError(f"API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise VisionError(f"could not reach the API: {e}") from e

    # A refusal is an HTTP 200 with no usable content, so stop_reason is
    # checked before content is read.
    if resp.stop_reason == "refusal":
        detail = getattr(resp.stop_details, "category", None)
        raise VisionError(f"the model declined to transcribe this crop ({detail})")
    if resp.stop_reason == "max_tokens":
        raise VisionError("the response hit max_tokens, so the spine list is "
                          "truncated and its positions cannot be trusted")

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise VisionError("the response carried no text block")
    try:
        spines = json.loads(text)["spines"]
    except (ValueError, KeyError, TypeError) as e:
        raise VisionError(f"could not parse the response as spines: {e}") from e

    spines.sort(key=lambda s: s.get("position") or 0)
    out = [_as_transcript(s, i) for i, s in enumerate(spines, 1)]

    if geometric_count is not None:
        verdict = count_check(out, geometric_count)
        # count_check always returns a verdict dict, so the mismatch is read
        # off `ok` rather than from the dict being truthy.
        if verdict.get("ok") is False:
            flag = (f"spine count disagrees with the geometric count: "
                    f"{verdict.get('vlm_count')} read vs "
                    f"{verdict.get('geometric_count')} regions, so positions "
                    f"on this shelf may be shifted")
            for s in out:
                s["note"] = "; ".join(filter(None, [s.get("note"), flag]))
    return out


def available() -> bool:
    """Whether a real transcription could be attempted at all."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return (pathlib.Path.home() / ".config" / "anthropic").exists()
