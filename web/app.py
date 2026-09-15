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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from shelfcat import export
from web import jobs

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = pathlib.Path(os.environ.get("SHELFMARK_WORK", ROOT / "work" / "jobs"))
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
        recent = jobs.recent(c, 10)
    return templates.TemplateResponse(
        request, "index.html",
        {"recent": recent, "fake": os.environ.get("SHELFCAT_FAKE_VISION", "")})


@app.post("/upload")
async def upload(request: Request, collection: str = Form("Shelf"),
                 photos: list[UploadFile] = File(...)):
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
            recent = jobs.recent(c, 10)
        return templates.TemplateResponse(
            request, "index.html",
            {"recent": recent, "error": "No usable images were uploaded.",
             "rejected": rejected,
             "fake": os.environ.get("SHELFCAT_FAKE_VISION", "")}, status_code=400)

    with db_con() as c:
        jobs.create(c, collection.strip() or "Shelf", work, job_id=jid,
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
        queue = jobs.review_queue(c, job_id)
        counts = jobs.review_counts(c, job_id)
    ids = [q["evidence_id"] for q in queue]
    # The current record may already be reviewed (arrived via a back button),
    # in which case it is not in the queue and there is no "n of m" for it.
    idx = ids.index(evidence_id) if evidence_id in ids else None
    nxt = ids[idx + 1] if idx is not None and idx + 1 < len(ids) else (
        ids[0] if ids and idx is None else None)
    prev = ids[idx - 1] if idx not in (None, 0) else None
    return templates.TemplateResponse(request, "review.html", {
        "job": job, "rec": rec, "counts": counts,
        "n": (idx + 1) if idx is not None else None, "of": len(ids),
        "next_id": nxt, "prev_id": prev,
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
        counts = jobs.review_counts(c, job_id)
        queue = jobs.review_queue(c, job_id)
    return JSONResponse({**res, "counts": counts,
                         "next_id": queue[0]["evidence_id"] if queue else None})


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
