"""shelfmark web: upload a shelf photograph, get a catalogue back.

Frontend choice: server-rendered Jinja2 templates plus a little vanilla JS.
The review screen shows one record at a time with three tap targets, so a
rendered page with fetch() for the actions gives instant response without a
bundler, a node toolchain, or a second language in the repository.

Nothing in this module reimplements the pipeline. It accepts files, creates a
job row, reads job rows, and serves the files the worker wrote.
"""
import contextlib
import os
import pathlib
import secrets
import shutil
from urllib.parse import quote

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from shelfcat import __version__, export
from web import jobs

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = pathlib.Path(os.environ.get("SHELFMARK_WORK", ROOT / "work" / "jobs"))
# Collection-wide exports are regenerated on demand and belong to no single
# batch of photographs, so they live beside the per-job directories.
COLLWORK = WORK.parent / "collections"
DB = pathlib.Path(os.environ.get("SHELFMARK_DB", ROOT / "work" / "shelfmark.db"))

# A phone photograph of a shelf is 2-5 MB. 40 MB leaves room for a 48 MP
# frame while still refusing an accidental video upload.
MAX_BYTES = 40 * 1024 * 1024
# One sitting is a few shelves. Past this it is a different workflow -- a bulk
# import someone should be doing in batches so they can review as they go --
# and the excess is reported, not silently truncated.
MAX_FILES = int(os.environ.get("SHELFMARK_MAX_FILES", "60"))
MAX_TOTAL_BYTES = int(os.environ.get("SHELFMARK_MAX_TOTAL_MB", "400")) * 1024 * 1024
# Keep this much room spare. Filling the disk mid-job corrupts the SQLite
# database, which loses the catalogue, not just the upload.
MIN_FREE_BYTES = 500 * 1024 * 1024
ALLOWED_SUFFIX = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff"}

# An optional shared passphrase. Not accounts, not multi-tenancy -- one code
# for the whole instance, off unless you set it.
#
# It exists because the vision backend spends real money per upload, so a
# public URL with no gate is an open tap on the owner's API key. Local use
# needs nothing; anything reachable from the internet needs this.
ACCESS_CODE = os.environ.get("SHELFMARK_ACCESS_CODE", "").strip()
COOKIE = "shelfmark_access"
OPEN_PATHS = ("/static/", "/sw.js", "/manifest.webmanifest", "/unlock", "/healthz")

templates = Jinja2Templates(directory=str(ROOT / "web" / "templates"))


def lifespan(app: FastAPI):
    WORK.mkdir(parents=True, exist_ok=True)
    DB.parent.mkdir(parents=True, exist_ok=True)
    jobs.connect(str(DB)).close()          # create the schema before serving
    stop = jobs.start_worker(str(DB))
    try:
        yield
    finally:
        stop()


app = FastAPI(title="shelfmark", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


@app.middleware("http")
async def require_access_code(request: Request, call_next):
    """Gate everything but the shell when SHELFMARK_ACCESS_CODE is set.

    Compared with compare_digest so a wrong guess takes the same time as a
    right one."""
    if not ACCESS_CODE or request.url.path.startswith(OPEN_PATHS):
        return await call_next(request)
    given = request.cookies.get(COOKIE, "")
    if secrets.compare_digest(given, ACCESS_CODE):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "locked"}, status_code=401)
    return RedirectResponse(f"/unlock?next={quote(str(request.url.path))}",
                            status_code=303)


@app.get("/unlock", response_class=HTMLResponse)
def unlock_form(request: Request, next: str = "/", bad: int = 0):
    if not ACCESS_CODE:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "unlock.html",
                                      {"next": next, "bad": bad})


@app.post("/unlock")
def unlock(code: str = Form(""), next: str = Form("/")):
    if not ACCESS_CODE or not secrets.compare_digest(code.strip(), ACCESS_CODE):
        return RedirectResponse(f"/unlock?next={quote(next)}&bad=1", status_code=303)
    # A local redirect only: `next` arrives from the query string, so an
    # absolute URL here would make this an open redirect.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    r = RedirectResponse(target, status_code=303)
    r.set_cookie(COOKIE, ACCESS_CODE, httponly=True, samesite="lax",
                 secure=os.environ.get("SHELFMARK_HTTPS", "") == "1",
                 max_age=60 * 60 * 24 * 30)
    return r


@app.get("/healthz", include_in_schema=False)
def healthz():
    """For the reverse proxy and for `systemctl` to tell working from wedged."""
    with db_con() as c:
        queued = c.execute("SELECT COUNT(*) AS n FROM jobs "
                           "WHERE state IN ('queued','running')").fetchone()["n"]
    return {"ok": True, "version": __version__, "active_jobs": queued,
            "vision": "fake" if os.environ.get("SHELFCAT_FAKE_VISION") else "real"}


def _unique(updir: pathlib.Path, name: str) -> pathlib.Path:
    """A destination that cannot overwrite an earlier upload.

    Phones name every photograph IMG_0001.jpg, so two albums in one sitting
    collide. Writing to the same path silently replaced a frame -- a whole
    shelf gone from the catalogue with nothing to show it had been there."""
    dest = updir / name
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for n in range(2, 1000):
        alt = updir / f"{stem}~{n}{suffix}"
        if not alt.exists():
            return alt
    raise HTTPException(409, f"too many uploads named {name}")


def _decodes(path: pathlib.Path) -> bool:
    """Whether the bytes are really the image the extension claims.

    A truncated or renamed file passes the extension check and then fails deep
    in the worker with a traceback about JPEG markers, which tells the
    librarian nothing. Checked here, it is one clear sentence."""
    from PIL import Image
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


@contextlib.contextmanager
def db_con():
    """A connection per use, opened in the thread that uses it.

    Deliberately NOT a FastAPI dependency. A sync `def` dependency is run in
    the threadpool while an `async def` handler runs on the event loop, so the
    connection would be created in one thread and used in another -- which
    sqlite3 refuses ("SQLite objects created in a thread can only be used in
    that same thread"). Opening it inside the handler makes the handler's
    sync-or-async colour irrelevant, so the bug cannot come back by someone
    later adding an async route."""
    c = jobs.connect(str(DB))
    try:
        yield c
    finally:
        c.close()


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """Served from the root so the worker's scope covers the whole app.

    A service worker can only control URLs under its own path, so one served
    from /static/sw.js would be scoped to /static/ and control nothing that
    matters."""
    return FileResponse(ROOT / "static" / "sw.js",
                        media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/",
                                 "Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(ROOT / "static" / "manifest.webmanifest",
                        media_type="application/manifest+json")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with db_con() as c:
        colls = jobs.collections(c, 20)
        pending = {k["id"]: jobs.review_counts(c, collection_id=k["id"])
                   for k in colls}
    return templates.TemplateResponse(
        request, "index.html",
        {"collections": colls, "pending": pending,
         "fake": os.environ.get("SHELFCAT_FAKE_VISION", "")})


@app.post("/upload")
async def upload(request: Request, collection: str = Form("Shelf"),
                 collection_id: str = Form(""), rows_per_shelf: int = Form(3),
                 photos: list[UploadFile] = File(...)):
    """Add a batch of photographs, either to a new collection or an existing one.

    `collection_id` wins when present: that is the "add more shelves" path, and
    the batch inherits the collection's shelf numbering rather than restarting
    at Shelf 1."""
    jid = jobs.new_id()
    work = WORK / jid
    updir = work / "upload"
    updir.mkdir(parents=True, exist_ok=True)

    # Refuse before writing anything if the disk cannot take it. Running out
    # of space mid-job can corrupt the SQLite database, which costs the
    # catalogue and not merely this upload.
    free = shutil.disk_usage(updir).free
    if free < MIN_FREE_BYTES:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(507, f"only {free // (1024*1024)} MB free on the "
                                 f"server; at least "
                                 f"{MIN_FREE_BYTES // (1024*1024)} MB is needed")

    # Rejections are reported, never silent: a librarian who uploads twelve
    # photographs and gets eleven frames must be told which one was dropped
    # and why, or the gap in the catalogue is invisible.
    kept, rejected = [], []
    total = 0
    for n, f in enumerate(photos):
        name = pathlib.Path(f.filename or "frame").name
        if n >= MAX_FILES:
            rejected.append(f"{name}: over the {MAX_FILES}-photograph limit for "
                            f"one sitting — upload the rest as a second batch "
                            f"and it will continue the shelf numbering")
            continue
        suffix = pathlib.Path(name).suffix.lower()
        if suffix not in ALLOWED_SUFFIX:
            rejected.append(f"{name}: not an image ({suffix or 'no extension'})")
            continue
        dest = _unique(updir, name)
        size = 0
        with dest.open("wb") as out:
            while chunk := await f.read(1 << 20):
                size += len(chunk)
                if size > MAX_BYTES or total + size > MAX_TOTAL_BYTES:
                    break
                out.write(chunk)
        total += size
        if size > MAX_BYTES:
            dest.unlink(missing_ok=True)
            rejected.append(f"{name}: larger than {MAX_BYTES // (1024*1024)} MB")
        elif total > MAX_TOTAL_BYTES:
            dest.unlink(missing_ok=True)
            rejected.append(f"{name}: this batch passed "
                            f"{MAX_TOTAL_BYTES // (1024*1024)} MB in total")
        elif size == 0:
            dest.unlink(missing_ok=True)
            rejected.append(f"{name}: the file was empty")
        elif not _decodes(dest):
            dest.unlink(missing_ok=True)
            rejected.append(f"{name}: not a readable {suffix.lstrip('.')} — the "
                            f"file may have been truncated in transfer")
        else:
            kept.append(dest.name)

    if not kept:
        shutil.rmtree(work, ignore_errors=True)
        with db_con() as c:
            colls = jobs.collections(c, 20)
            pending = {k["id"]: jobs.review_counts(c, collection_id=k["id"])
                       for k in colls}
        return templates.TemplateResponse(
            request, "index.html",
            {"collections": colls, "pending": pending,
             "error": ("No usable images were uploaded."
                       if rejected else
                       "No photographs were attached."), "rejected": rejected,
             "fake": os.environ.get("SHELFCAT_FAKE_VISION", "")}, status_code=400)

    with db_con() as c:
        coll = jobs.get_collection(c, collection_id) if collection_id else None
        if coll is None:
            cid = jobs.create_collection(c, collection, rows_per_shelf)
            coll = jobs.get_collection(c, cid)
        jobs.create(c, coll["name"], work, job_id=jid,
                    collection_id=coll["id"],
                    warnings=[f"rejected on upload - {r}" for r in rejected])
    return RedirectResponse(f"/job/{jid}", status_code=303)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with db_con() as c:
        j = jobs.get(c, job_id)
    if not j:
        raise HTTPException(404, "no such job")
    j["warnings"] = jobs.warning_lines(j)
    j["stages"] = jobs.STAGES
    return JSONResponse(j)


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    with db_con() as c:
        j = jobs.get(c, job_id)
    if not j:
        raise HTTPException(404, "no such job")
    return templates.TemplateResponse(request, "job.html",
                                      {"job": j, "stages": jobs.STAGES})


@app.get("/job/{job_id}/records", response_class=HTMLResponse)
def job_records(request: Request, job_id: str):
    with db_con() as c:
        j = jobs.get(c, job_id)
        if not j:
            raise HTTPException(404, "no such job")
        recs = jobs.records_for(c, job_id)
    return templates.TemplateResponse(
        request, "records.html",
        {"job": j, "records": recs,
         "n_review": sum(1 for r in recs if r["needs_review"]),
         "warnings": jobs.warning_lines(j)})


# --- collections -----------------------------------------------------------

@app.get("/collection/{cid}", response_class=HTMLResponse)
def collection_page(request: Request, cid: str):
    with db_con() as c:
        coll = jobs.get_collection(c, cid)
        if not coll:
            raise HTTPException(404, "no such collection")
        batches = jobs.collection_jobs(c, cid)
        counts = jobs.review_counts(c, collection_id=cid)
        work = jobs.shelf_work(c, collection_id=cid)
        warns = [w for b in batches for w in jobs.warning_lines(b)]
    return templates.TemplateResponse(request, "collection.html", {
        "coll": coll, "batches": batches, "counts": counts,
        "n_layers": sum(b["n_layers"] or 0 for b in batches),
        "n_frames": sum(b["n_frames"] or 0 for b in batches),
        "shelf_work": work, "warnings": warns,
        "active": any(b["state"] in ("queued", "running") for b in batches),
        "fake": os.environ.get("SHELFCAT_FAKE_VISION", "")})


@app.get("/collection/{cid}/review", response_class=HTMLResponse)
def collection_review(cid: str):
    """Resume. Jumps to the first undecided volume anywhere in the collection,
    whichever batch of photographs it arrived in."""
    with db_con() as c:
        if not jobs.get_collection(c, cid):
            raise HTTPException(404, "no such collection")
        queue = jobs.review_queue(c, collection_id=cid)
    if not queue:
        return RedirectResponse(f"/collection/{cid}?reviewed=1", status_code=303)
    nxt = queue[0]
    return RedirectResponse(f"/job/{nxt['job_id']}/review/{nxt['evidence_id']}",
                            status_code=303)


def _collection_export(cid, coll, recs, fmt):
    name, media = DOWNLOADS[fmt]
    out = COLLWORK / cid
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    if fmt == "csv":
        export.to_csv(path, recs)
    elif fmt == "mrc":
        export.to_marc(path, recs, org="ShelfMark",
                       library=(coll["name"] or "MAIN")[:10])
    else:
        raise HTTPException(409, "the workbook is written per batch of "
                                 "photographs; use CSV or MARC for a whole "
                                 "collection")
    stem = "".join(ch if ch.isalnum() else "-" for ch in coll["name"]).strip("-")
    return FileResponse(path, media_type=media,
                        filename=f"{stem or 'catalogue'}{path.suffix}")


@app.get("/collection/{cid}/download/{fmt}")
def collection_download(cid: str, fmt: str):
    """The whole collection in one file, regenerated from current records."""
    if fmt not in DOWNLOADS:
        raise HTTPException(404, "unknown format")
    with db_con() as c:
        coll = jobs.get_collection(c, cid)
        if not coll:
            raise HTTPException(404, "no such collection")
        recs = jobs.records_for(c, collection_id=cid)
    if not recs:
        raise HTTPException(409, "this collection has no catalogued volumes yet")
    return _collection_export(cid, coll, recs, fmt)


@app.get("/collection/{cid}/worklist", response_class=HTMLResponse)
def collection_worklist(request: Request, cid: str):
    with db_con() as c:
        coll = jobs.get_collection(c, cid)
        if not coll:
            raise HTTPException(404, "no such collection")
        work = jobs.shelf_work(c, collection_id=cid)
    return templates.TemplateResponse(request, "worklist.html", {
        "job": {"id": cid, "collection": coll["name"]}, "work": work,
        "scope": "collection", "cid": cid})


# --- review ----------------------------------------------------------------

@app.get("/job/{job_id}/review", response_class=HTMLResponse)
def review_start(job_id: str):
    """Jump to the first volume still needing a decision."""
    with db_con() as c:
        if not jobs.get(c, job_id):
            raise HTTPException(404, "no such job")
        queue = jobs.review_queue(c, job_id)
    if not queue:
        return RedirectResponse(f"/job/{job_id}/records?reviewed=1", status_code=303)
    return RedirectResponse(f"/job/{job_id}/review/{queue[0]['evidence_id']}",
                            status_code=303)


@app.get("/job/{job_id}/review/{evidence_id}", response_class=HTMLResponse)
def review_one(request: Request, job_id: str, evidence_id: int):
    with db_con() as c:
        job = jobs.get(c, job_id)
        if not job:
            raise HTTPException(404, "no such job")
        rec = jobs.record_detail(c, job_id, evidence_id)
        if not rec:
            raise HTTPException(404, "no such record in this job")
        # The queue spans the collection, not just this batch, so a review
        # runs straight on into the shelves photographed yesterday instead of
        # stopping at an upload boundary the librarian does not think in.
        cid = job.get("collection_id")
        queue = jobs.review_queue(c, collection_id=cid) if cid \
            else jobs.review_queue(c, job_id)
        counts = jobs.review_counts(c, collection_id=cid) if cid \
            else jobs.review_counts(c, job_id)
        coll = jobs.get_collection(c, cid) if cid else None
        qmap = {q["evidence_id"]: q["job_id"] for q in queue}
        _p, spine_meta = jobs.spine_window(job["work_dir"], rec["spine"]["reads"])
    ids = [q["evidence_id"] for q in queue]
    # The current record may already be reviewed (arrived via a back button),
    # in which case it is not in the queue and there is no "n of m" for it.
    idx = ids.index(evidence_id) if evidence_id in ids else None
    nxt = ids[idx + 1] if idx is not None and idx + 1 < len(ids) else (
        ids[0] if ids and idx is None else None)
    prev = ids[idx - 1] if idx not in (None, 0) else None
    return templates.TemplateResponse(request, "review.html", {
        "job": job, "coll": coll, "rec": rec, "counts": counts,
        "n": (idx + 1) if idx is not None else None, "of": len(ids),
        "next_id": nxt, "next_job": qmap.get(nxt, job_id),
        "prev_id": prev, "prev_job": qmap.get(prev, job_id),
        "spine_exact": not spine_meta.get("approximate", True),
        "review_below": export.REVIEW_BELOW})


@app.get("/job/{job_id}/spine/{evidence_id}.jpg")
def spine_image(job_id: str, evidence_id: int, w: int = 1):
    with db_con() as c:
        job = jobs.get(c, job_id)
        if not job:
            raise HTTPException(404, "no such job")
        rec = jobs.record_detail(c, job_id, evidence_id)
    if not rec:
        raise HTTPException(404, "no such record")
    path, _meta = jobs.spine_window(job["work_dir"], rec["spine"]["reads"],
                                    neighbours=max(0, min(w, 4)))
    if not path:
        raise HTTPException(404, "no crop available for this spine")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/job/{job_id}/review/{evidence_id}")
def review_action(job_id: str, evidence_id: int, body: dict = Body(...)):
    action = (body or {}).get("action")
    with db_con() as c:
        try:
            res = jobs.apply_review(
                c, job_id, evidence_id, action,
                claim_id=(body or {}).get("claim_id"),
                fields=(body or {}).get("fields"),
                reviewer=(body or {}).get("reviewer") or "reviewer")
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        except PermissionError as e:
            raise HTTPException(409, str(e))
        job = jobs.get(c, job_id)
        cid = (job or {}).get("collection_id")
        counts = jobs.review_counts(c, collection_id=cid) if cid \
            else jobs.review_counts(c, job_id)
        queue = jobs.review_queue(c, collection_id=cid) if cid \
            else jobs.review_queue(c, job_id)
    nxt = queue[0] if queue else None
    return JSONResponse({**res, "counts": counts,
                         "next_id": nxt["evidence_id"] if nxt else None,
                         "next_job": nxt["job_id"] if nxt else None,
                         "collection_id": cid})


@app.get("/job/{job_id}/worklist", response_class=HTMLResponse)
def worklist(request: Request, job_id: str):
    """The list you print, or carry on a phone, and walk the shelves with."""
    with db_con() as c:
        job = jobs.get(c, job_id)
        if not job:
            raise HTTPException(404, "no such job")
        work = jobs.shelf_work(c, job_id)
    return templates.TemplateResponse(request, "worklist.html",
                                      {"job": job, "work": work})


def _worklist_csv(work, label):
    """One writer for both scopes, so a batch's list and a collection's list
    cannot describe the same task differently."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["task", "shelf_id", "position", "call_number", "spine_read",
                "frames", "record_id"])
    for group in work["unreadable"]:
        for r in group["rows"]:
            w.writerow(["read this spine", r["shelf_id"], r["position"],
                        export.call_number(r), "", r["frames"], r["record_id"]])
    for g in work["volume_check"]:
        for r in g["members"]:
            w.writerow(["read the volume number", r["shelf_id"], r["position"],
                        export.call_number(r), r["raw_title"], r["frames"],
                        r["record_id"]])
    stem = "".join(ch if ch.isalnum() else "-" for ch in label).strip("-")
    return Response(buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{stem or "shelf"}-worklist.csv"'})


@app.get("/job/{job_id}/worklist.csv")
def worklist_csv(job_id: str):
    with db_con() as c:
        job = jobs.get(c, job_id)
        if not job:
            raise HTTPException(404, "no such job")
        work = jobs.shelf_work(c, job_id)
    return _worklist_csv(work, job["collection"])


@app.get("/collection/{cid}/worklist.csv")
def collection_worklist_csv(cid: str):
    with db_con() as c:
        coll = jobs.get_collection(c, cid)
        if not coll:
            raise HTTPException(404, "no such collection")
        work = jobs.shelf_work(c, collection_id=cid)
    return _worklist_csv(work, coll["name"])


DOWNLOADS = {
    "mrc": ("catalogue.mrc", "application/marc"),
    "csv": ("catalogue.csv", "text/csv"),
    "xlsx": ("catalogue.xlsx",
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
}


@app.get("/job/{job_id}/download/{fmt}")
def download(job_id: str, fmt: str):
    if fmt not in DOWNLOADS:
        raise HTTPException(404, "unknown format")
    with db_con() as c:
        j = jobs.get(c, job_id)
    if not j:
        raise HTTPException(404, "no such job")
    name, media = DOWNLOADS[fmt]
    path = pathlib.Path(j["work_dir"]) / "out" / name
    if fmt in ("csv", "mrc"):
        # Regenerated from the current records on every download. Writing
        # these once at the end of the job would hand the librarian a file
        # that silently predates every correction they just made.
        with db_con() as c:
            recs = jobs.records_for(c, job_id)
        if recs:
            path.parent.mkdir(parents=True, exist_ok=True)
            if fmt == "csv":
                export.to_csv(path, recs)
            else:
                export.to_marc(path, recs, org="ShelfMark",
                               library=(j["collection"] or "MAIN")[:10])
    if not path.exists():
        raise HTTPException(409, f"{name} has not been written yet "
                                 f"(job is {j['state']}, stage {j['stage']})")
    stem = "".join(ch if ch.isalnum() else "-" for ch in j["collection"]).strip("-")
    return FileResponse(path, media_type=media,
                        filename=f"{stem or 'catalogue'}-{job_id}{path.suffix}")
