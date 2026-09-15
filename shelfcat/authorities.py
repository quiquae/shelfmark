"""Authority lookup and match scoring.

Two entry paths with very different trust levels:

  isbn_lookup(isbn)      -- identity is already known from a decoded barcode.
                            The authority only supplies descriptive metadata.
                            Result tier: green.
  text_search(title,...) -- identity is a hypothesis. The authority returns
                            candidates that must be scored and reviewed.
                            Result tier: amber at best.

NETWORK STATUS: the HTTP calls below could NOT be verified from the build
container (egress proxy blocked openlibrary.org and googleapis.com on
2026-09-01). Run `python -m shelfcat.authorities --selftest` on a networked
machine before trusting this module. The scoring functions are pure and are
tested offline.
"""
import re, unicodedata
from difflib import SequenceMatcher

UA = {"User-Agent": "shelfcat/0.1 (library inventory; contact set in config)"}

_ARTICLES = ("the ", "a ", "an ", "le ", "la ", "les ", "der ", "die ", "das ", "il ", "el ")


def normalise(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for art in _ARTICLES:
        if s.startswith(art):
            s = s[len(art):]
            break
    return s


def surname(author: str) -> str:
    a = normalise(author)
    if not a:
        return ""
    if "," in author:
        return normalise(author.split(",")[0])
    return a.split()[-1]


def score_match(read_title, read_author, cand_title, cand_author) -> float:
    """0..1. Title dominates; author surname is a strong confirmer.

    Deliberately conservative: a strong title match with a CONTRADICTED author
    is capped, because that is the signature of a different work with a
    similar name, which is exactly the error a reviewer would miss."""
    t = SequenceMatcher(None, normalise(read_title), normalise(cand_title)).ratio()
    if not read_author or not cand_author:
        return round(t * 0.85, 3)          # unconfirmed author -> never full marks
    a_read, a_cand = surname(read_author), surname(cand_author)
    if not a_read or not a_cand:
        return round(t * 0.85, 3)
    a = SequenceMatcher(None, a_read, a_cand).ratio()
    if a >= 0.85:
        return round(min(1.0, 0.75 * t + 0.25 * a + 0.05), 3)
    if a < 0.5:
        return round(min(t, 0.55) * 0.8, 3)   # author contradiction: cap hard
    return round(0.75 * t + 0.25 * a, 3)


GREEN_MIN, AMBER_MIN = 0.92, 0.72


def tier_for(scores: list[float]) -> str:
    """Tier from the candidate score distribution. A close second candidate
    downgrades the result even when the top score is high -- ambiguity is the
    risk, not just weakness."""
    if not scores:
        return "black"
    s = sorted(scores, reverse=True)
    top = s[0]
    gap = top - s[1] if len(s) > 1 else 1.0
    if top >= GREEN_MIN and gap >= 0.10:
        return "amber"       # text evidence can never reach green; barcode only
    if top >= AMBER_MIN:
        return "amber" if gap >= 0.05 else "red"
    return "red"
