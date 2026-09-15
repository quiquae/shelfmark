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
import shutil

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from shelfcat import export
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
ALLOWED_SUFFIX = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff"}

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

    # Rejections are reported, never silent: a librarian who uploads twelve
    # photographs and gets eleven frames must be told which one was dropped
    # and why, or the gap in the catalogue is invisible.
    kept, rejected = [], []
    for f in photos:
        name = pathlib.Path(f.filename or "frame").name
        suffix = pathlib.Path(name).suffix.lower()
        if suffix not in ALLOWED_SUFFIX:
            rejected.append(f"{name}: not an image ({suffix or 'no extension'})")
            continue
        dest = updir / name
        size = 0
        with dest.open("wb") as out:
            while chunk := await f.read(1 << 20):
                size += len(chunk)
                if size > MAX_BYTES:
                    break
                out.write(chunk)
        if size > MAX_BYTES:
            dest.unlink(missing_ok=True)
            rejected.append(f"{name}: larger than {MAX_BYTES // (1024*1024)} MB")
        else:
            kept.append(name)

    if not kept:
        shutil.rmtree(work, ignore_errors=True)
        with db_con() as c:
            colls = jobs.collections(c, 20)
            pending = {k["id"]: jobs.review_counts(c, collection_id=k["id"])
                       for k in colls}
        return templates.TemplateResponse(
            request, "index.html",
            {"collections": colls, "pending": pending,
             "error": "No usable images were uploaded.", "rejected": rejected,
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
