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
import threading
import time
import traceback
import uuid

from shelfcat import authorities, db, export, pipeline, spines
from shelfcat.excel import confidence

JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id          TEXT PRIMARY KEY,
  collection  TEXT NOT NULL,
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

def connect(db_path):
    con = db.connect(db_path)
    con.executescript(JOBS_SCHEMA)
    return con


def new_id():
    return uuid.uuid4().hex[:12]


def create(con, collection, work_dir, job_id=None, warnings=None):
    """`warnings` carries anything already known at submission time -- files
    rejected on upload, most often. _run() appends to it rather than replacing
    it, so a rejection recorded here survives into the finished job."""
    jid = job_id or new_id()
    con.execute("INSERT INTO jobs (id, collection, work_dir, stage, message, "
                "warnings) VALUES (?,?,?,?,?,?)",
                (jid, collection, str(work_dir), "queued", "waiting for the worker",
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


def records_for(con, job_id):
    """Every volume in the job, in shelf order, joined to its accepted claim.

    A LEFT JOIN, not an inner one: a record whose claim_id is null is an
    unresolved volume and must still appear. This query is the reason the
    export can promise that nothing is dropped."""
    rows = con.execute("""
        SELECT r.id AS record_row, r.evidence_id, r.claim_id, r.tier,
               r.shelf_id, r.position, r.title, r.authors, r.year,
               r.publisher, r.isbn13, r.reviewed_by, r.note,
               e.payload, e.detector, c.score, c.authority, c.raw AS claim_raw
          FROM records r
          JOIN evidence e  ON e.id = r.evidence_id
          JOIN job_items j ON j.evidence_id = r.evidence_id
     LEFT JOIN claims c    ON c.id = r.claim_id
         WHERE j.job_id = ?
      ORDER BY r.shelf_id, r.position
    """, (job_id,)).fetchall()
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
        d["source"] = d.pop("authority") or "unresolved"
        d["needs_review"] = export.needs_review(d)
        out.append(d)
    return out


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


def _stage_catalogue(con, jid, work, tdir, collection):
    _set(con, jid, stage="stitch", progress=0.55,
         message="merging overlaps and assigning shelf rows")
    (work / "out").mkdir(parents=True, exist_ok=True)   # excel.build will not
    return pipeline.catalogue(str(tdir), str(work / "manifest.json"),
                              str(work / "out" / "catalogue.xlsx"),
                              project=collection)


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
               "flags": list(book.flags), "variants": list(book.variants)}
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
    res = _stage_catalogue(con, jid, work, tdir, job["collection"])
    warnings += res.get("location_warnings", [])

    layers, assignments = res["layers"], res["assignments"]
    n_books = sum(len(l) for l in layers)
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


def start_worker(db_path, poll=0.5):
    """One daemon thread holding one connection for its whole life.

    The connection is opened once, not per poll: reconnecting twice a second
    would re-run the schema script on every tick for no benefit."""
    stop = threading.Event()

    def loop():
        con = connect(db_path)
        try:
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
