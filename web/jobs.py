"""Background work: a jobs table and exactly one worker thread.

Why not Celery. The queue needs three things -- survive a restart, be readable
by a status endpoint, and run one CPU-heavy job at a time. A table in the
SQLite database already present does all three in a page of code, with no
broker to run alongside the web process. Redis or Celery become right at the
point where jobs must run on more than one machine or in parallel; neither is
true yet, and the claim below is already atomic so adding a second worker is
a configuration change rather than a rewrite.

Why a thread rather than FastAPI's BackgroundTasks. BackgroundTasks runs after
the response inside the request worker, so there is nowhere to hang a status,
and a long OpenCV pass would occupy a request slot. OpenCV releases the GIL
for the operations that matter, so one daemon thread is enough.

The pipeline is NOT reimplemented here. This module calls pipeline.prepare,
spines.transcribe_frame, pipeline.catalogue, authorities.resolve and export.*
in order, and records what each returned.
"""
import json
import pathlib
import re
import sqlite3
import threading
import time
import traceback
import uuid

from shelfcat import authorities, db, export, pipeline, spines
from shelfcat.excel import confidence

JOBS_SCHEMA = """
-- A collection is the library, or the room, or the bookcase: the thing whose
-- shelf numbering is continuous. A job is one batch of photographs added to
-- it. Photographing a whole library in one sitting is not how this gets used.
CREATE TABLE IF NOT EXISTS collections (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  rows_per_shelf INTEGER NOT NULL DEFAULT 3,
  created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jobs (
  id            TEXT PRIMARY KEY,
  collection    TEXT NOT NULL,          -- the name, kept for display
  collection_id TEXT REFERENCES collections(id),
  n_layers      INTEGER DEFAULT 0,      -- shelf layers this batch contributed
  state       TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|failed
  stage       TEXT,
  progress    REAL DEFAULT 0.0,
  message     TEXT,
  work_dir    TEXT NOT NULL,
  n_frames    INTEGER DEFAULT 0,
  n_books     INTEGER DEFAULT 0,
  n_review    INTEGER DEFAULT 0,
  warnings    TEXT,
  error       TEXT,
  created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_coll ON jobs(collection_id, created_at);

-- Job ownership lives here rather than as a column on db.evidence, so the
-- provenance schema stays exactly as it was designed.
CREATE TABLE IF NOT EXISTS job_items (
  job_id      TEXT NOT NULL REFERENCES jobs(id),
  evidence_id INTEGER NOT NULL REFERENCES evidence(id),
  PRIMARY KEY (job_id, evidence_id)
);
CREATE INDEX IF NOT EXISTS idx_ji_job ON job_items(job_id);
"""

STAGES = ["ingest", "crops", "vision", "stitch", "resolve", "export"]

# Extensions pipeline.prepare can actually decode. Anything else in the upload
# directory is moved aside rather than left to crash cv2 mid-run.
IMAGE_SUFFIX = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff"}


# --- job rows --------------------------------------------------------------

def _add_columns(con, table, columns):
    """ALTER TABLE ... ADD COLUMN, skipping what is already there.

    SQLite has no ADD COLUMN IF NOT EXISTS, and a database created before
    these columns existed must keep working rather than requiring the
    librarian to throw their catalogue away."""
    have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def connect(db_path):
    con = db.connect(db_path)
    # Order matters. A database written before collections existed has a jobs
    # table without collection_id, and JOBS_SCHEMA declares an index over that
    # column -- so the migration has to run first. On a fresh database the
    # CREATE TABLE above already declares it and the migration is a no-op.
    try:
        _add_columns(con, "jobs", {"collection_id": "TEXT",
                                   "n_layers": "INTEGER DEFAULT 0"})
    except sqlite3.OperationalError:
        pass                                   # no jobs table yet: fresh db
    con.executescript(JOBS_SCHEMA)
    con.commit()
    return con


# --- collections -----------------------------------------------------------

def create_collection(con, name, rows_per_shelf=3):
    cid = uuid.uuid4().hex[:12]
    con.execute("INSERT INTO collections (id, name, rows_per_shelf) VALUES (?,?,?)",
                (cid, (name or "Collection").strip() or "Collection",
                 max(1, int(rows_per_shelf or 3))))
    con.commit()
    return cid


def get_collection(con, collection_id):
    r = con.execute("SELECT * FROM collections WHERE id=?", (collection_id,)).fetchone()
    return dict(r) if r else None


def collection_jobs(con, collection_id):
    rows = con.execute("SELECT * FROM jobs WHERE collection_id=? "
                       "ORDER BY created_at, rowid", (collection_id,)).fetchall()
    return [dict(r) for r in rows]


def collections(con, limit=50):
    """Every collection with enough summary to decide what to do next."""
    rows = con.execute("""
        SELECT c.*,
               COUNT(j.id)                         AS n_jobs,
               COALESCE(SUM(j.n_frames), 0)        AS n_frames,
               COALESCE(SUM(j.n_books), 0)         AS n_books,
               COALESCE(SUM(j.n_layers), 0)        AS n_layers,
               MAX(j.created_at)                   AS last_added,
               SUM(j.state = 'running' OR j.state = 'queued') AS n_active,
               SUM(j.state = 'failed')             AS n_failed
          FROM collections c
     LEFT JOIN jobs j ON j.collection_id = c.id
      GROUP BY c.id
      ORDER BY COALESCE(MAX(j.created_at), c.created_at) DESC
         LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def layer_offset(con, collection_id, exclude_job=None):
    """How many shelf layers this collection already holds.

    Counted from finished jobs only: a job still running has not settled its
    layer count, and guessing would misnumber every shelf after it."""
    if not collection_id:
        return 0
    sql = ("SELECT COALESCE(SUM(n_layers), 0) AS n FROM jobs "
           "WHERE collection_id=? AND state='done'")
    args = [collection_id]
    if exclude_job:
        sql += " AND id<>?"
        args.append(exclude_job)
    return int(con.execute(sql, args).fetchone()["n"] or 0)


def _scope(con, job_id=None, collection_id=None):
    """Resolve a request to the job ids it covers.

    One entry point for both scopes, so the queue, the exports and the shelf
    worklist cannot drift apart in what they consider "everything"."""
    if job_id:
        return [job_id]
    if collection_id:
        return [j["id"] for j in collection_jobs(con, collection_id)]
    return []


def new_id():
    return uuid.uuid4().hex[:12]


def create(con, collection, work_dir, job_id=None, warnings=None,
           collection_id=None):
    """`warnings` carries anything already known at submission time -- files
    rejected on upload, most often. _run() appends to it rather than replacing
    it, so a rejection recorded here survives into the finished job."""
    jid = job_id or new_id()
    con.execute("INSERT INTO jobs (id, collection, collection_id, work_dir, "
                "stage, message, warnings) VALUES (?,?,?,?,?,?,?)",
                (jid, collection, collection_id, str(work_dir), "queued",
                 "waiting for the worker",
                 json.dumps(list(warnings)) if warnings else None))
    con.commit()
    return jid


def get(con, job_id):
    r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(r) if r else None


def recent(con, limit=20):
    rows = con.execute("SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC "
                       "LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _set(con, job_id, **kw):
    con.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in kw)} WHERE id=?",
                (*kw.values(), job_id))
    con.commit()


_ROW_RANK = {"TOP": 0, "MIDDLE": 1, "BOTTOM": 2}


def shelf_sort_key(rec):
    """Walking order, not alphabetical order.

    shelf_id is text, so ORDER BY shelf_id puts S10-TOP before S2-TOP and a
    collection silently stops being in shelf order at its tenth shelf.
    Sorting on the parsed number, the row's physical rank and the position
    reproduces the actual walk."""
    m = re.match(r"S(\d+)-(.+)$", str(rec.get("shelf_id") or ""))
    if not m:
        return (10 ** 9, 99, rec.get("position") or 0)
    row = m.group(2).upper()
    rank = _ROW_RANK.get(row)
    if rank is None:
        n = re.search(r"(\d+)", row)
        rank = int(n.group(1)) if n else 98
    return (int(m.group(1)), rank, rec.get("position") or 0)


def warning_lines(job) -> list[str]:
    """Warnings arrive in two shapes: plain strings from ingest and this
    module, and {kind, detail, severity} dicts from location.validate. Flatten
    to display strings without losing the severity, so the page never shows a
    raw Python dict to a librarian."""
    out = []
    for w in json.loads((job or {}).get("warnings") or "[]"):
        if isinstance(w, dict):
            detail = w.get("detail") or w.get("kind") or str(w)
            sev = w.get("severity")
            out.append(f"{detail}" + (f"  [{sev}]" if sev else ""))
        else:
            out.append(str(w))
    return out


def records_for(con, job_id=None, *, collection_id=None):
    """Every volume in scope, in walking order, joined to its accepted claim.

    Scope is one job or a whole collection, resolved through _scope so the
    queue, the exports and the shelf worklist cannot disagree about what
    "everything" means.

    A LEFT JOIN, not an inner one: a record whose claim_id is null is an
    unresolved volume and must still appear. This query is the reason the
    export can promise that nothing is dropped."""
    ids = _scope(con, job_id, collection_id)
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = con.execute(f"""
        SELECT r.id AS record_row, r.evidence_id, r.claim_id, r.tier,
               r.shelf_id, r.position, r.title, r.authors, r.year,
               r.publisher, r.isbn13, r.reviewed_by, r.note,
               e.payload, e.detector, c.score, c.authority, c.raw AS claim_raw
          FROM records r
          JOIN evidence e  ON e.id = r.evidence_id
          JOIN job_items j ON j.evidence_id = r.evidence_id
     LEFT JOIN claims c    ON c.id = r.claim_id
         WHERE j.job_id IN ({ph})
    """, ids).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        p = json.loads(d.pop("payload") or "{}")
        d["record_id"] = f"SM{d['evidence_id']:08d}"
        d["raw_title"] = p.get("title")
        d["raw_author"] = p.get("author")
        d["raw_volume"] = p.get("volume")
        d["confidence"] = p.get("confidence")
        d["n_reads"] = p.get("n_reads")
        d["frames"] = ",".join(p.get("frames") or [])
        d["flags"] = ", ".join(p.get("flags") or [])
        d["variants"] = p.get("variants") or []
        d["resolve_error"] = p.get("resolve_error")
        claim = {}
        try:
            claim = json.loads(d.pop("claim_raw") or "{}") or {}
        except (TypeError, ValueError):
            claim = {}
        d["ddc"] = claim.get("ddc")
        d["lcc"] = claim.get("lcc")
        d["subjects"] = claim.get("subjects") or []
        d["n_editions"] = claim.get("n_editions") or 0
        d["reads"] = p.get("reads") or []
        d["machine_tier"] = p.get("machine_tier")
        d["source"] = d.pop("authority") or "unresolved"
        d["reviewed"] = bool(d.get("reviewed_by"))
        d["needs_review"] = export.needs_review(d) and not d["reviewed"]
        out.append(d)
    out.sort(key=shelf_sort_key)
    return out


# --- work that can only be done at the shelf -------------------------------

def shelf_work(con, job_id=None, *, collection_id=None):
    """Everything no amount of model quality will fix, in walking order.

    Two kinds, and both end with a person standing in front of the books:

    unreadable -- the spine could not be read at all. 170 of the 1,017 volumes
      in the reference run, 17%. That is a floor, not a defect: a dark spine
      with no text is not recoverable from any photograph.

    volume_check -- several books share a title and none of their volume
      numbers could be read, so their order within the set is unverified.
      Reported instead of a duplicate-copy claim, because the action is "read
      the volume numbers" and not "find the second copy".

    Ordered by shelf then position so the list matches the walk, and each row
    links to its review screen: the point is to stand at the shelf with a
    phone and type in what the camera could not read."""
    recs = records_for(con, job_id, collection_id=collection_id)
    unreadable = [r for r in recs if not (r.get("raw_title") or "").strip()]

    by_title = {}
    for r in recs:
        t = (r.get("raw_title") or "").strip().lower()
        if t and not (r.get("raw_volume") or "").strip():
            by_title.setdefault(t, []).append(r)
    groups = [{"title": v[0]["raw_title"], "members": v}
              for v in by_title.values() if len(v) >= 2]
    groups.sort(key=lambda g: (-len(g["members"]), g["title"] or ""))

    def shelves(rows):
        out = {}
        for r in rows:
            out.setdefault(r["shelf_id"] or "UNSHELVED", []).append(r)
        return [{"shelf": k, "rows": sorted(v, key=shelf_sort_key)}
                for k, v in sorted(out.items(),
                                   key=lambda kv: shelf_sort_key(kv[1][0]))]

    return {"unreadable": shelves(unreadable),
            "n_unreadable": len(unreadable),
            "volume_check": groups,
            "n_volume_check": sum(len(g["members"]) for g in groups),
            "total": len(recs)}


# --- spine imagery ---------------------------------------------------------

def spine_window(work_dir, reads, neighbours: int = 1):
    """Render a slice of the band crop containing one spine.

Two paths, and the caller is told which one it got.

    With a `bbox` -- [x0, y0, x1, y1] fractions of the crop, which vision.py
    returns -- the spine's position is known, and the window is that box
    widened by a spine-width so the neighbours stay visible.

    Without one -- the 79 transcripts of 1 Sep 2026 carry an ordinal and
    nothing else -- the position is estimated as (ordinal + 0.5) / n across
    the crop, an assumption whose error grows with how much spine widths vary.
    That window is cut wide enough to contain the neighbours and is reported
    as `approximate`, because a tight crop confidently one spine off is worse
    than three spines and an honest caption.
    """
    from PIL import Image

    work = pathlib.Path(work_dir)
    for read in reads or []:
        frame, index = read.get("image"), read.get("index")
        if not frame or index is None:
            continue
        tpath = work / "transcripts" / f"{frame}.json"
        if not tpath.exists():
            continue
        try:
            spines_ = json.loads(tpath.read_text()).get("spines") or []
        except ValueError:
            continue
        # Read.index is documented 0-based but load_transcripts fills it from
        # the transcript's `i`, which is 1-based in all 79 real transcripts.
        # stitch only ever uses index for ordering, so the mismatch never
        # mattered there. Match on `i` and fall back to position, so this
        # works whichever convention a vision backend follows.
        target = next((sp for sp in spines_ if sp.get("i") == index), None)
        if target is None and 0 <= index < len(spines_):
            target = spines_[index]
        if target is None:
            continue
        crop = target.get("crop")
        if not crop or not pathlib.Path(crop).exists():
            continue
        peers = [sp for sp in spines_ if sp.get("crop") == crop]
        try:
            pos = peers.index(target)
        except ValueError:
            pos = 0
        n = max(1, len(peers))

        # A real backend returns bbox as [x0, y0, x1, y1] fractions of the
        # crop, in which case the spine's location is known and there is
        # nothing to estimate. The window is widened by one spine-width so the
        # reviewer still sees what sits either side, but it is centred on the
        # truth.
        bbox = target.get("bbox")
        exact = (isinstance(bbox, (list, tuple)) and len(bbox) == 4
                 and all(isinstance(v, (int, float)) for v in bbox)
                 and bbox[2] > bbox[0])

        out = work / "spines" / f"{frame}_{index}_{neighbours}_{'x' if exact else 'e'}.jpg"
        meta = {"frame": frame, "pos": pos + 1, "of": n, "approximate": not exact}
        if out.exists():
            return out, meta
        im = Image.open(crop)
        if exact:
            pad = (bbox[2] - bbox[0]) * max(0, neighbours)
            lo = max(0.0, bbox[0] - pad)
            hi = min(1.0, bbox[2] + pad)
        else:
            lo = max(0.0, (pos - neighbours) / n)
            hi = min(1.0, (pos + 1 + neighbours) / n)
        box = (int(lo * im.width), 0,
               max(int(hi * im.width), int(lo * im.width) + 8), im.height)
        out.parent.mkdir(parents=True, exist_ok=True)
        im.crop(box).save(out, "JPEG", quality=88, optimize=True)
        return out, meta
    return None, {}


# --- review ----------------------------------------------------------------

REVIEW_SQL = """
    SELECT r.evidence_id, r.shelf_id, r.position, r.tier, r.reviewed_by,
           e.payload, c.score, c.authority, j.job_id
      FROM records r
      JOIN evidence e  ON e.id = r.evidence_id
      JOIN job_items j ON j.evidence_id = r.evidence_id
 LEFT JOIN claims c    ON c.id = r.claim_id
     WHERE j.job_id IN ({ph})
"""


def _review_rows(con, job_id=None, collection_id=None):
    ids = _scope(con, job_id, collection_id)
    if not ids:
        return []
    sql = REVIEW_SQL.format(ph=",".join("?" * len(ids)))
    rows = [dict(r) for r in con.execute(sql, ids).fetchall()]
    rows.sort(key=shelf_sort_key)
    return rows


def review_queue(con, job_id=None, *, collection_id=None):
    """Every volume a machine is not entitled to assert, in walking order.

    Walking order, not confidence order: a reviewer works left to right along
    the actual shelf, and sending them round the room to save a few seconds of
    model uncertainty is a false economy.

    Given a collection, the queue spans every batch of photographs in it, so an
    interrupted review resumes where it stopped rather than at the start of
    whichever upload happened to be open."""
    out = []
    for d in _review_rows(con, job_id, collection_id):
        p = json.loads(d.get("payload") or "{}")
        rec = {"evidence_id": d["evidence_id"], "tier": d["tier"],
               "score": d["score"], "source": d["authority"] or "unresolved",
               "reviewed_by": d["reviewed_by"]}
        if export.needs_review(rec) and not d["reviewed_by"]:
            out.append({"evidence_id": d["evidence_id"], "job_id": d["job_id"],
                        "shelf_id": d["shelf_id"], "position": d["position"],
                        "raw_title": p.get("title")})
    return out


def review_counts(con, job_id=None, *, collection_id=None):
    total = reviewed = pending = 0
    for d in _review_rows(con, job_id, collection_id):
        total += 1
        if d["reviewed_by"]:
            reviewed += 1
        elif export.needs_review({"tier": d["tier"], "score": d["score"],
                                  "source": d["authority"] or "unresolved"}):
            pending += 1
    return {"total": total, "reviewed": reviewed, "pending": pending,
            "done": total - pending}


def record_detail(con, job_id, evidence_id, *, collection_id=None):
    """One volume with every candidate the authorities offered, so a reviewer
    can see what was rejected and not only what was chosen."""
    ids = _scope(con, job_id, collection_id)
    if not ids:
        return None
    ph = ",".join("?" * len(ids))
    row = con.execute(f"""
        SELECT r.*, e.payload, e.detector, j.job_id
          FROM records r
          JOIN evidence e  ON e.id = r.evidence_id
          JOIN job_items j ON j.evidence_id = r.evidence_id
         WHERE j.job_id IN ({ph}) AND r.evidence_id = ?
    """, (*ids, evidence_id)).fetchone()
    if not row:
        return None
    d = dict(row)
    p = json.loads(d.pop("payload") or "{}")
    cands = []
    for c in con.execute(
            "SELECT id, authority, authority_id, title, authors, year, "
            "publisher, isbn13, score, raw FROM claims WHERE evidence_id=? "
            "ORDER BY score DESC", (evidence_id,)).fetchall():
        cd = dict(c)
        try:
            extra = json.loads(cd.pop("raw") or "{}") or {}
        except (TypeError, ValueError):
            extra = {}
        cd["ddc"] = extra.get("ddc")
        cd["lcc"] = extra.get("lcc")
        cd["subjects"] = extra.get("subjects") or []
        cd["n_editions"] = extra.get("n_editions") or 1
        cd["chosen"] = cd["id"] == d.get("claim_id")
        cands.append(cd)
    d["spine"] = {"title": p.get("title"), "author": p.get("author"),
                  "volume": p.get("volume"), "legibility": p.get("legibility"),
                  "confidence": p.get("confidence"), "n_reads": p.get("n_reads"),
                  "frames": p.get("frames") or [], "flags": p.get("flags") or [],
                  "variants": p.get("variants") or [], "reads": p.get("reads") or []}
    d["machine_tier"] = p.get("machine_tier")
    d["candidates"] = cands
    return d


def apply_review(con, job_id, evidence_id, action, *, claim_id=None,
                 fields=None, reviewer="reviewer"):
    """Accept, correct, skip or undo one volume.

    Accepting promotes the row to green, which db.set_tier permits only with a
    named reviewer -- a machine pass may never reach green on text evidence.
    Undo restores the tier the machine originally assigned, which is why that
    verdict is kept in the evidence payload rather than overwritten."""
    cur = con.execute("SELECT * FROM records r JOIN job_items j "
                      "ON j.evidence_id = r.evidence_id "
                      "WHERE j.job_id=? AND r.evidence_id=?",
                      (job_id, evidence_id)).fetchone()
    if not cur:
        raise KeyError(f"no record {evidence_id} in job {job_id}")

    if action == "skip":
        # Seen and deliberately left alone. The tier does not move, so the
        # volume still reads as unverified in the export -- skipping is not
        # approval, and pretending otherwise is how a bad record ships.
        con.execute("UPDATE records SET reviewed_by=?, reviewed_at="
                    "CURRENT_TIMESTAMP, note=? WHERE evidence_id=?",
                    (reviewer, "skipped at review", evidence_id))
        con.commit()
        return {"tier": cur["tier"], "action": "skip"}

    if action == "undo":
        payload = json.loads(con.execute(
            "SELECT payload FROM evidence WHERE id=?",
            (evidence_id,)).fetchone()["payload"] or "{}")
        con.execute("UPDATE records SET reviewed_by=NULL, reviewed_at=NULL, "
                    "note=NULL, tier=? WHERE evidence_id=?",
                    (payload.get("machine_tier") or cur["tier"], evidence_id))
        con.commit()
        return {"tier": payload.get("machine_tier"), "action": "undo"}

    if action not in ("accept", "edit"):
        raise ValueError(f"unknown action {action!r}")

    if action == "accept" and claim_id is not None:
        c = con.execute("SELECT * FROM claims WHERE id=? AND evidence_id=?",
                        (claim_id, evidence_id)).fetchone()
        if not c:
            raise KeyError(f"claim {claim_id} does not belong to {evidence_id}")
        con.execute("UPDATE records SET claim_id=?, title=?, authors=?, year=?, "
                    "publisher=?, isbn13=? WHERE evidence_id=?",
                    (claim_id, c["title"], c["authors"], c["year"],
                     c["publisher"], c["isbn13"], evidence_id))
    if fields:
        allowed = ("title", "authors", "year", "publisher", "isbn13")
        sets = {k: (fields.get(k) or None) for k in allowed if k in fields}
        if sets:
            con.execute(f"UPDATE records SET {', '.join(f'{k}=?' for k in sets)} "
                        f"WHERE evidence_id=?", (*sets.values(), evidence_id))

    title = con.execute("SELECT title FROM records WHERE evidence_id=?",
                        (evidence_id,)).fetchone()["title"]
    # A volume with no title is not verified, whatever the reviewer pressed.
    target = "green" if (title or "").strip() else "red"
    con.commit()
    db.set_tier(con, evidence_id, target, reviewed_by=reviewer)
    return {"tier": target, "action": action}



# --- stages ----------------------------------------------------------------

def _shelf_id(assignment):
    return f"S{assignment['shelf']}-{str(assignment['row']).upper()}"


def _sift_uploads(updir, parked):
    """Move anything undecodable out of the way before prepare() globs the
    upload directory.

    `parked` must sit OUTSIDE updir: prepare() globs "*", so a parking
    directory created inside it becomes the next thing PIL is asked to open.

    Files are moved, never deleted, and every one is named in the job's
    warnings -- a librarian who uploads twelve photographs and gets eleven
    frames has to be told which one went missing and why."""
    aside = []
    for p in sorted(updir.iterdir()):
        if p.is_dir():
            aside.append(f"{p.name}: is a directory, skipped")
        elif p.suffix.lower() not in IMAGE_SUFFIX:
            parked.mkdir(parents=True, exist_ok=True)
            p.rename(parked / p.name)
            aside.append(f"{p.name}: not a decodable image, set aside")
    return aside


def _stage_prepare(con, jid, work):
    _set(con, jid, stage="ingest", progress=0.05,
         message="reading frames, applying EXIF orientation")
    aside = _sift_uploads(work / "upload", work / "rejected")
    manifest, warnings = pipeline.prepare(str(work / "upload"), str(work), pattern="*")
    if not manifest:
        raise RuntimeError("no readable image frames were uploaded")
    soft = [m["name"] for m in manifest if m.get("quality") != "ok"]
    if soft:
        warnings.append(f"{len(soft)} frame(s) below the quality gate: "
                        f"{', '.join(soft[:6])} - spines may be unreadable")
    _set(con, jid, stage="crops", progress=0.2, n_frames=len(manifest),
         message=f"{len(manifest)} frame(s) banded and cropped")
    return manifest, warnings + aside


def _stage_vision(con, jid, work, manifest):
    tdir = work / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    seam_warnings = []
    for n, m in enumerate(manifest, 1):
        t = spines.transcribe_frame(m["name"], m["crops"])
        (tdir / f"{m['name']}.json").write_text(json.dumps(t, indent=1))
        for s in t.get("crop_seams", []):
            if not s.get("overlap"):
                seam_warnings.append(
                    f"{m['name']}: crops overlap geometrically but no shared "
                    f"spines were found at {s['crop']}; positions across that "
                    f"seam are unverified")
        _set(con, jid, stage="vision", progress=0.2 + 0.3 * n / len(manifest),
             message=f"transcribed {n}/{len(manifest)} frame(s)")
    return tdir, seam_warnings


def _stage_catalogue(con, jid, work, tdir, collection, *, offset=0, rows_per_shelf=3):
    _set(con, jid, stage="stitch", progress=0.55,
         message=(f"merging overlaps, continuing from layer {offset + 1}"
                  if offset else "merging overlaps and assigning shelf rows"))
    (work / "out").mkdir(parents=True, exist_ok=True)   # excel.build will not
    return pipeline.catalogue(str(tdir), str(work / "manifest.json"),
                              str(work / "out" / "catalogue.xlsx"),
                              project=collection, rows_per_shelf=rows_per_shelf,
                              layer_offset=offset)


def _record_images(con, manifest):
    con.executemany(
        "INSERT OR IGNORE INTO images (sha256, path, captured_at, width, "
        "height, seq, quality, quality_note) VALUES (?,?,?,?,?,?,?,?)",
        [(m["sha256"], m["source"], m.get("captured_at"), m.get("width"),
          m.get("height"), m.get("seq"), m.get("quality"), m.get("quality_note"))
         for m in manifest])
    con.commit()


def _resolve_book(book, cache):
    """(candidates, tier, error). Cached by the text actually searched, so a
    forty-volume set costs one request rather than forty.

    An illegible spine is never sent to an authority: there is no text to
    match, and a query on an empty title returns arbitrary popular books that
    would arrive looking like real candidates."""
    if not book.title:
        return [], "black", None
    key = (book.title, book.author or "")
    if key not in cache:
        try:
            cands, tier, _ = authorities.resolve(book.title, book.author)
            cache[key] = (cands, tier, None)
        except Exception as e:                      # network is never fatal
            cache[key] = ([], "red", f"{type(e).__name__}: {e}")
    return cache[key]


def _persist_book(con, jid, book, shelf_id, position, sha, detector, cache):
    """One volume: evidence, its candidate claims, and the record row.

    Resolution happens BEFORE the evidence insert so that a resolver failure
    is inside the payload that gets written, rather than being attached to a
    dict that has already been serialised."""
    cands, tier, err = _resolve_book(book, cache)

    payload = {"title": book.title, "author": book.author, "volume": book.volume,
               "legibility": book.legibility, "confidence": confidence(book),
               "n_reads": book.n_reads, "frames": book.sources,
               "flags": list(book.flags), "variants": list(book.variants),
               # where this spine was read, so the review screen can show the
               # right crop, and what the machine decided, so a review can be
               # undone without losing the original verdict
               "reads": [{"image": r.image, "index": r.index,
                          "at_edge": bool(r.at_edge)} for r in book.reads],
               "machine_tier": tier}
    if err:
        payload["resolve_error"] = err

    eid = con.execute(
        "INSERT INTO evidence (image_sha, kind, position, payload, detector) "
        "VALUES (?,?,?,?,?)",
        (sha, "spine", position, json.dumps(payload), detector)).lastrowid
    con.execute("INSERT INTO job_items (job_id, evidence_id) VALUES (?,?)",
                (jid, eid))

    claim_id = None
    for c in cands[:5]:
        cid = con.execute(
            "INSERT INTO claims (evidence_id, authority, authority_id, title, "
            "authors, year, publisher, isbn13, score, raw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid, c["authority"], c.get("authority_id"), c.get("title"),
             c.get("authors"), c.get("year"), c.get("publisher"),
             c.get("isbn13"), c.get("score"),
             # Classification is derived across the merged editions, so it is
             # stored explicitly rather than left to be re-parsed out of the
             # authority response. No schema change: db.claims.raw is JSON.
             json.dumps({"ddc": c.get("ddc"), "lcc": c.get("lcc"),
                         "subjects": c.get("subjects"),
                         "n_editions": c.get("n_editions", 1),
                         "authority": c.get("raw")}, default=str))).lastrowid
        if claim_id is None:
            claim_id = cid                          # candidates arrive sorted
    top = cands[0] if cands else {}
    con.execute(
        "INSERT INTO records (evidence_id, claim_id, tier, shelf_id, position, "
        "title, authors, year, publisher, isbn13) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (eid, claim_id, tier, shelf_id, position, top.get("title"),
         top.get("authors"), top.get("year"), top.get("publisher"),
         top.get("isbn13")))
    return eid


def _run(con, job):
    jid, work = job["id"], pathlib.Path(job["work_dir"])
    authorities.reset_breaker()
    prior = json.loads(job.get("warnings") or "[]")

    manifest, warnings = _stage_prepare(con, jid, work)
    warnings = prior + warnings
    tdir, seam_warnings = _stage_vision(con, jid, work, manifest)
    warnings += seam_warnings

    # Shelf numbering continues from whatever this collection already holds.
    # Without it a second batch of photographs restarts at "Shelf 1 top" and
    # every call number in it collides with the first batch's.
    coll = get_collection(con, job.get("collection_id")) or {}
    rows_per_shelf = int(coll.get("rows_per_shelf") or 3)
    offset = layer_offset(con, job.get("collection_id"), exclude_job=jid)

    res = _stage_catalogue(con, jid, work, tdir, job["collection"],
                           offset=offset, rows_per_shelf=rows_per_shelf)
    warnings += res.get("location_warnings", [])

    layers, assignments = res["layers"], res["assignments"]
    n_books = sum(len(l) for l in layers)
    # A job that finds nothing must fail, not succeed with "0 volume(s)",
    # which reads like it worked. The usual cause is a photograph the shelf is
    # not actually in, or one taken end-on down a row.
    if not n_books:
        raise RuntimeError(
            f"no book spines were found in {len(manifest)} photograph(s). "
            f"Is the shelf square-on and filling the frame? A photograph taken "
            f"down the length of a row, or of a closed cupboard, gives nothing "
            f"to read.")
    # Recorded before resolution so a later batch can offset from it even if
    # this job fails part-way through.
    _set(con, jid, n_layers=len(layers))
    if offset:
        warnings.append(
            f"shelf numbering continued from layer {offset + 1} of this "
            f"collection; this batch added {len(layers)} more")
    _record_images(con, manifest)

    sha_by_frame = {m["name"]: m["sha256"] for m in manifest}
    fallback_sha = manifest[0]["sha256"]
    detector = "vlm:fake" if spines.fake_vision_enabled() else "vlm:real"
    cache, done = {}, 0
    step = max(1, n_books // 20)

    for li, layer in enumerate(layers):
        shelf = (_shelf_id(assignments[li]) if li < len(assignments)
                 else f"S?-LAYER{li + 1}")
        for position, book in enumerate(layer, 1):
            frames = book.sources
            # image_sha is NOT NULL, so a book whose frame name is somehow
            # absent from the manifest is attributed to the first frame rather
            # than being dropped for a foreign-key error.
            sha = next((sha_by_frame[f] for f in frames if f in sha_by_frame),
                       fallback_sha)
            _persist_book(con, jid, book, shelf, position, sha, detector, cache)
            done += 1
            if done % step == 0 or done == n_books:
                _set(con, jid, stage="resolve",
                     progress=0.55 + 0.35 * done / max(1, n_books),
                     message=f"resolved {done}/{n_books} volume(s)")
    con.commit()

    dark = authorities.breaker_state()
    if dark:
        warnings.append(f"authority unavailable for part of this run: "
                        f"{', '.join(dark)} - affected volumes are flagged, "
                        f"not silently unmatched")

    _set(con, jid, stage="export", progress=0.95, message="writing CSV and MARC")
    recs = records_for(con, jid)
    export.to_csv(work / "out" / "catalogue.csv", recs)
    marc = export.to_marc(work / "out" / "catalogue.mrc", recs,
                          org="ShelfMark", library=(job["collection"] or "MAIN")[:10])

    _set(con, jid, state="done", stage="export", progress=1.0,
         n_books=len(recs), n_review=marc["needs_review"],
         message=f"{len(recs)} volume(s), {marc['needs_review']} need review",
         warnings=json.dumps(warnings),
         finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))


# --- worker ----------------------------------------------------------------

def _claim(con):
    """Atomically take the oldest queued job. RETURNING makes this safe for
    more than one worker, so the single-worker default is a choice and not an
    assumption baked into the queue."""
    row = con.execute("""
        UPDATE jobs SET state='running', stage='ingest', message='starting'
         WHERE id = (SELECT id FROM jobs WHERE state='queued'
                      ORDER BY created_at, rowid LIMIT 1)
        RETURNING *
    """).fetchone()
    con.commit()
    return dict(row) if row else None


def run_claimed(con, job):
    try:
        _run(con, job)
    except Exception as e:
        _set(con, job["id"], state="failed", message=str(e)[:300],
             error=traceback.format_exc()[-4000:],
             finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    return job["id"]


def run_once(db_path):
    """Claim and run one queued job on a fresh connection. Used by tests."""
    con = connect(db_path)
    try:
        job = _claim(con)
        return run_claimed(con, job) if job else None
    finally:
        con.close()


def recover_stale(con):
    """Deal with jobs the process was killed in the middle of.

    A deploy, a restart or an OOM leaves a row marked `running` that no worker
    will ever touch again, and the librarian watches a progress bar that has
    stopped moving with nothing to tell them why.

    Two outcomes, and the split is about not destroying human work:

    - nothing persisted yet: requeue it. Every pipeline stage is independently
      re-runnable, so this is free.
    - records already exist: fail it with a reason. Re-running would insert a
      second set of evidence and double-count the shelf, and the records may
      already carry review decisions. Re-uploading is the honest fix.
    """
    rows = con.execute("SELECT id FROM jobs WHERE state IN ('running')").fetchall()
    requeued, failed = [], []
    for r in rows:
        jid = r["id"]
        n = con.execute("SELECT COUNT(*) AS n FROM job_items WHERE job_id=?",
                        (jid,)).fetchone()["n"]
        if n:
            _set(con, jid, state="failed",
                 message=f"interrupted by a restart after {n} volume(s) were "
                         f"already recorded; re-upload this batch",
                 finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            failed.append(jid)
        else:
            _set(con, jid, state="queued", stage="queued", progress=0.0,
                 message="requeued after a restart interrupted it")
            requeued.append(jid)
    if requeued or failed:
        print(f"shelfmark: recovered {len(requeued)} interrupted job(s), "
              f"failed {len(failed)} that had already written records",
              flush=True)
    return {"requeued": requeued, "failed": failed}


def start_worker(db_path, poll=0.5):
    """One daemon thread holding one connection for its whole life.

    The connection is opened once, not per poll: reconnecting twice a second
    would re-run the schema script on every tick for no benefit."""
    stop = threading.Event()

    def loop():
        con = connect(db_path)
        try:
            recover_stale(con)
            while not stop.is_set():
                try:
                    job = _claim(con)
                except Exception:
                    traceback.print_exc()
                    stop.wait(poll)
                    continue
                if job is None:
                    stop.wait(poll)
                else:
                    run_claimed(con, job)
        finally:
            con.close()

    threading.Thread(target=loop, name="shelfcat-worker", daemon=True).start()
    return stop.set
