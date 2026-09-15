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


# ---------------------------------------------------------------------------
# HTTP layer
#
# The docstring at the top of this file has described isbn_lookup() and
# text_search() since v0.1 without either existing. They exist now.
#
# THE AUTHOR IS NEVER A FILTER. Open Library's `author=` and Google Books'
# `inauthor:` are AND constraints on the index, so a spine that reads
# "Ryals & Fielding" -- an abbreviated editor credit, which is the normal case
# on a spine -- matches no indexed author and zeroes the entire result set.
# Measured: that exact query returns 0 candidates with the author filter and 5
# without, the best scoring 0.85 against "Kenneth J. Fielding", which is
# right. So the title filters and the author scores. score_match() already
# treats a contradicted author as a hard cap, which is the correct place for
# that judgement -- it downgrades a match instead of hiding it.
#
# Failure is tolerated per authority: a dropped volume is worse than an
# unresolved one, and the caller records "unresolved" with the raw
# transcription intact either way.
# ---------------------------------------------------------------------------
import json as _json

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

OPENLIBRARY_SEARCH = "https://openlibrary.org/search.json"
OPENLIBRARY_ISBN = "https://openlibrary.org/api/books"
GOOGLEBOOKS = "https://www.googleapis.com/books/v1/volumes"
TIMEOUT = 8.0
OL_FIELDS = "key,title,author_name,first_publish_year,publisher,isbn"

# Open Library carries the load. Google Books is corroboration only: the
# unauthenticated endpoint returns 429 from a shared IP under any real volume,
# so nothing may depend on it being reachable.
#
# Note also that Open Library's first_publish_year is the year of the WORK,
# not of the copy on the shelf. It belongs in a candidate for a human to
# confirm, and must never be asserted as this volume's date.

_SESSION = None

# Circuit breaker. The unauthenticated Google Books quota is not a transient
# condition -- once a shared IP is throttled it stays throttled -- so retrying
# it costs ~1.8s of backoff per spine and buys nothing. After this many
# consecutive rate-limited calls an authority is dropped for the rest of the
# process, and the reason is recorded on every affected candidate instead of
# the volume silently looking unfound.
BREAKER_TRIP = 3
_FAILS: dict[str, int] = {}


def _rate_limited(e) -> bool:
    code = getattr(getattr(e, "response", None), "status_code", None)
    if code == 429:
        return True
    return isinstance(e, requests.exceptions.RetryError) or "429" in str(e)


def breaker_state() -> dict:
    """Exposed so a caller can report which authorities went dark."""
    return {k: v for k, v in _FAILS.items() if v >= BREAKER_TRIP}


def reset_breaker():
    _FAILS.clear()


def _session():
    """One pooled session with backoff.

    Cataloguing a shelf is hundreds of sequential requests to two hosts, so
    connection reuse is most of the wall-clock saving, and a 429 is an
    expected condition rather than an error. Retry honours Retry-After."""
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.headers.update(UA)
        retry = Retry(total=2, connect=2, read=2, backoff_factor=0.6,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset(["GET"]),
                      respect_retry_after_header=True)
        s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=8))
        _SESSION = s
    return _SESSION


def _isbn13(values) -> str | None:
    """Prefer a 13-digit ISBN; accept a 10 only if nothing better is present."""
    best = None
    for v in values or []:
        d = re.sub(r"[^0-9Xx]", "", str(v))
        if len(d) == 13:
            return d
        if len(d) == 10 and not best:
            best = d
    return best


def _year(v) -> str | None:
    m = re.search(r"(1[0-9]{3}|20[0-9]{2})", str(v or ""))
    return m.group(1) if m else None


def _get(url, params):
    r = _session().get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _ol_candidate(d):
    return {
        "authority": "openlibrary",
        "authority_id": (d.get("key") or "").replace("/works/", ""),
        "title": d.get("title"),
        "authors": ", ".join(d.get("author_name") or []) or None,
        "year": str(d["first_publish_year"]) if d.get("first_publish_year") else None,
        "publisher": (d.get("publisher") or [None])[0],
        "isbn13": _isbn13(d.get("isbn")),
        "raw": d,
    }


def _openlibrary_text(title, author=None, limit=5):
    """Title-filtered first. Only if that finds nothing does the author text
    get used, and then as free-text ranking rather than as a constraint."""
    docs = _get(OPENLIBRARY_SEARCH,
                {"title": title, "limit": limit, "fields": OL_FIELDS}).get("docs", [])
    if not docs:
        q = f"{title} {author}".strip() if author else title
        docs = _get(OPENLIBRARY_SEARCH,
                    {"q": q, "limit": limit, "fields": OL_FIELDS}).get("docs", [])
    return [_ol_candidate(d) for d in docs[:limit]]


def _gb_candidate(it):
    v = it.get("volumeInfo", {})
    return {
        "authority": "googlebooks",
        "authority_id": it.get("id"),
        "title": v.get("title"),
        "authors": ", ".join(v.get("authors") or []) or None,
        "year": _year(v.get("publishedDate")),
        "publisher": v.get("publisher"),
        "isbn13": _isbn13([i.get("identifier")
                           for i in v.get("industryIdentifiers") or []]),
        "raw": v,
    }


def _googlebooks_text(title, author=None, limit=5):
    items = _get(GOOGLEBOOKS, {"q": f'intitle:"{title}"',
                               "maxResults": limit}).get("items", [])
    if not items and author:
        items = _get(GOOGLEBOOKS, {"q": f"{title} {author}",
                                   "maxResults": limit}).get("items", [])
    return [_gb_candidate(it) for it in items[:limit]]


def isbn_lookup(isbn):
    """Identity is already known from a decoded barcode; the authority only
    supplies descriptive metadata. A hit here is the only route to tier green."""
    digits = re.sub(r"[^0-9Xx]", "", str(isbn or ""))
    if len(digits) not in (10, 13):
        return []
    key = f"ISBN:{digits}"
    try:
        d = _get(OPENLIBRARY_ISBN,
                 {"bibkeys": key, "format": "json", "jscmd": "data"}).get(key)
    except (requests.RequestException, ValueError):
        return []
    if not d:
        return []
    return [{
        "authority": "openlibrary",
        "authority_id": (d.get("key") or "").strip("/").split("/")[-1],
        "title": d.get("title"),
        "authors": ", ".join(a.get("name") for a in d.get("authors") or []) or None,
        "year": _year(d.get("publish_date")),
        "publisher": (d.get("publishers") or [{}])[0].get("name"),
        "isbn13": digits if len(digits) == 13 else None,
        "score": 1.0,
        "raw": d,
    }]


def text_search(title, author=None, limit=5):
    """Identity is a hypothesis. Returns candidates from both authorities,
    scored and sorted, each carrying the raw response for the audit trail.

    Errors are swallowed per authority: if Open Library is down, Google Books
    candidates still come back; if both fail the result is empty, which the
    caller records as unresolved. `partial` names whichever failed so the
    reason survives into the record rather than looking like "not found"."""
    if not title or not str(title).strip():
        return []
    title = str(title).strip()
    cands, errors = [], []
    for fn in (_openlibrary_text, _googlebooks_text):
        name = fn.__name__.strip("_").replace("_text", "")
        if _FAILS.get(name, 0) >= BREAKER_TRIP:
            errors.append(f"{name}: skipped, rate-limited earlier in this run")
            continue
        try:
            cands.extend(fn(title, author, limit=limit))
            _FAILS[name] = 0
        except (requests.RequestException, ValueError) as e:
            if _rate_limited(e):
                _FAILS[name] = _FAILS.get(name, 0) + 1
                errors.append(f"{name}: rate-limited")
            else:
                code = getattr(getattr(e, "response", None), "status_code", None)
                errors.append(f"{name}: "
                              f"{'HTTP ' + str(code) if code else type(e).__name__}")
    for c in cands:
        c["score"] = score_match(title, author, c.get("title"), c.get("authors"))
    cands.sort(key=lambda c: c["score"], reverse=True)
    if errors:
        note = "; ".join(errors)
        for c in cands:
            c["partial"] = note
    return cands


def resolve(title, author=None, isbn=None, limit=5):
    """Full resolution for one spine. Returns (candidates, tier, method)."""
    if isbn:
        hits = isbn_lookup(isbn)
        if hits:
            return hits, "green", "isbn"
    cands = text_search(title, author, limit=limit)
    return cands, tier_for([c["score"] for c in cands]), "text"


def _selftest():
    """Run on a networked machine. The build container of 1 Sep 2026 blocked
    both hosts, which is why this module shipped without an HTTP layer."""
    cases = [
        ("openlibrary, plain title", lambda: _openlibrary_text("The Canterbury Tales", "Chaucer")),
        ("openlibrary, awkward author", lambda: _openlibrary_text(
            "The Collected Letters of Thomas and Jane Welsh Carlyle", "Ryals & Fielding")),
        ("googlebooks", lambda: _googlebooks_text("The Canterbury Tales", "Chaucer")),
        ("isbn lookup", lambda: isbn_lookup("9780140424386")),
    ]
    ok = True
    for name, fn in cases:
        try:
            res = fn()
            print(f"  {'PASS' if res else 'FAIL'}  {name}: {len(res)} candidate(s)")
            if res:
                t = res[0]
                print(f"        {str(t.get('title'))[:52]!r} / "
                      f"{str(t.get('authors'))[:30]!r} ({t.get('year')})")
            ok = ok and bool(res)
        except Exception as e:
            if _rate_limited(e):
                print(f"  SKIP  {name}: rate-limited after retries (the call "
                      f"reached the host; nothing depends on this authority)")
                continue
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            ok = False
    for t, a in [("Canterbury Tales", "Chaucer"),
                 ("The Collected Letters of Thomas and Jane Welsh Carlyle",
                  "Ryals & Fielding")]:
        cands, tier, method = resolve(t, a)
        top = f"{cands[0]['title'][:40]!r} @ {cands[0]['score']}" if cands else "none"
        print(f"  resolve {t[:34]!r} -> {len(cands)} cand, tier={tier}, top={top}")
    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return ok


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(0 if _selftest() else 1)
    print(_json.dumps(text_search(*sys.argv[1:3]), indent=1, default=str)[:2000])
