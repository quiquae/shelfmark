"""Storage layer.

Three-table separation is the whole point of this design:

  images    immutable record of what was captured (never edited)
  evidence  observations extracted from an image -- a decoded barcode, a
            spine crop with read text. Always traceable to pixels.
  claims    candidate identifications produced by an authority lookup,
            with a score and the method that produced them. Many per evidence.
  records   the resolved catalogue row. Points at the evidence and the
            accepted claim. Carries a tier that can never be silently raised.

Nothing is ever overwritten in place: re-running the pipeline adds evidence
and claims, it does not mutate what a human already decided.
"""
import sqlite3, json, pathlib

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS images (
  sha256      TEXT PRIMARY KEY,
  path        TEXT NOT NULL,
  captured_at TEXT,
  width       INTEGER,
  height      INTEGER,
  shelf_id    TEXT,            -- from marker barcode or filename convention
  seq         INTEGER,         -- capture order within shelf
  quality     TEXT,            -- ok | soft | low_res | unusable
  quality_note TEXT,
  ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS evidence (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  image_sha   TEXT NOT NULL REFERENCES images(sha256),
  kind        TEXT NOT NULL,   -- barcode | spine | marker
  bbox        TEXT,            -- json [x,y,w,h] in source pixels
  position    INTEGER,         -- ordinal on the shelf, left to right
  payload     TEXT NOT NULL,   -- json: decoded value, or read spine fields
  detector    TEXT NOT NULL,   -- zbar | zxing | vlm:<model> | scanner:hid
  detector_ok INTEGER DEFAULT 1,
  created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ev_img ON evidence(image_sha);
CREATE INDEX IF NOT EXISTS idx_ev_kind ON evidence(kind);

CREATE TABLE IF NOT EXISTS claims (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  evidence_id INTEGER NOT NULL REFERENCES evidence(id),
  authority   TEXT NOT NULL,   -- openlibrary | googlebooks | loc
  authority_id TEXT,           -- OLID / volumeId / LCCN
  title       TEXT, authors TEXT, year TEXT, publisher TEXT, isbn13 TEXT,
  score       REAL,            -- 0..1 match confidence
  raw         TEXT,            -- json of the authority response
  created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cl_ev ON claims(evidence_id);

CREATE TABLE IF NOT EXISTS records (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  evidence_id INTEGER NOT NULL UNIQUE REFERENCES evidence(id),
  claim_id    INTEGER REFERENCES claims(id),
  tier        TEXT NOT NULL,   -- green | amber | red | black
  shelf_id    TEXT, position INTEGER,
  title TEXT, authors TEXT, year TEXT, publisher TEXT, isbn13 TEXT,
  reviewed_by TEXT, reviewed_at TEXT, note TEXT
);
CREATE INDEX IF NOT EXISTS idx_rec_tier ON records(tier);
CREATE INDEX IF NOT EXISTS idx_rec_shelf ON records(shelf_id, position);
"""

TIER_RANK = {"black": 0, "red": 1, "amber": 2, "green": 3}


def connect(path="catalog.db"):
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def set_tier(con, evidence_id, new_tier, *, reviewed_by=None, force=False):
    """Tiers move up only through human review. A machine pass may never
    promote a row it did not verify deterministically."""
    cur = con.execute("SELECT tier FROM records WHERE evidence_id=?", (evidence_id,))
    row = cur.fetchone()
    if row is None:
        raise KeyError(f"no record for evidence {evidence_id}")
    old = row["tier"]
    if TIER_RANK[new_tier] > TIER_RANK[old] and not (reviewed_by or force):
        raise PermissionError(
            f"refusing silent promotion {old} -> {new_tier} for evidence "
            f"{evidence_id}: promotion requires reviewed_by or force=True")
    con.execute("UPDATE records SET tier=?, reviewed_by=?, "
                "reviewed_at=CURRENT_TIMESTAMP WHERE evidence_id=?",
                (new_tier, reviewed_by, evidence_id))
    con.commit()
    return old, new_tier
