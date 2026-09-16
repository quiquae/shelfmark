"""M4: the things that decide whether this survives a real deployment.

Upload guards, the optional access code, health, and recovery from being
killed mid-job. No network, no server. Exits non-zero on failure.
"""
import io
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
_tmp = tempfile.TemporaryDirectory()
TMP = pathlib.Path(_tmp.name)
os.environ["SHELFMARK_WORK"] = str(TMP / "jobs")
os.environ["SHELFMARK_DB"] = str(TMP / "h.db")
os.environ["SHELFCAT_FAKE_VISION"] = "1"
os.environ["SHELFMARK_MAX_FILES"] = "3"
os.environ["SHELFMARK_MAX_TOTAL_MB"] = "1"

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from shelfcat import authorities
from web import app as webapp, jobs

fails = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"   {detail}"))
    if not cond:
        fails.append(label)


authorities.resolve = lambda t, a=None, isbn=None, limit=5: (
    [{"authority": "stub", "authority_id": "x", "title": t, "authors": a,
      "year": "1900", "publisher": "P", "isbn13": None, "score": 0.95,
      "ddc": None, "lcc": None, "subjects": None, "raw": {}}], "amber", "text")


def shelf_jpeg(px=1800):
    im = Image.new("RGB", (px, int(px * 0.45)), (40, 30, 24))
    d = ImageDraw.Draw(im)
    h = im.height
    d.rectangle([0, h * 0.18, im.width, h * 0.82], fill=(24, 20, 18))
    x = 60
    while x < im.width - 120:
        d.rectangle([x, h * 0.19, x + 54, h * 0.81], fill=(90 + x % 120, 70, 60),
                    outline=(10, 10, 10), width=3)
        x += 62
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return buf.getvalue()


GOOD = shelf_jpeg()
print(f"fixture: {len(GOOD)//1024}KB shelf photograph\n")

print("=== 1. upload guards ===")
with TestClient(webapp.app) as c:
    def up(files, **data):
        return c.post("/upload", data={"collection": "Guards", **data},
                      files=files, follow_redirects=False)

    r = up([("photos", ("a.pdf", b"%PDF-1.4 not an image", "application/pdf"))])
    check("a non-image is refused with 400", r.status_code == 400, str(r.status_code))
    check("and it says which file and why",
          "a.pdf" in r.text and "not an image" in r.text)

    r = up([("photos", ("broken.jpg", b"\xff\xd8\xff" + b"\x00" * 200, "image/jpeg"))])
    check("a truncated JPEG is caught at upload, not deep in the worker",
          r.status_code == 400 and "truncated" in r.text, str(r.status_code))

    r = up([("photos", ("empty.jpg", b"", "image/jpeg"))])
    check("an empty file is refused", r.status_code == 400 and "empty" in r.text)

    # SHELFMARK_MAX_FILES=3, so the fourth is reported rather than dropped
    r = up([("photos", (f"f{i}.jpg", GOOD, "image/jpeg")) for i in range(4)])
    check("over the file limit still redirects (the first 3 are kept)",
          r.status_code == 303, str(r.status_code))
    jid = r.headers["location"].rsplit("/", 1)[-1]
    con = jobs.connect(os.environ["SHELFMARK_DB"])
    warns = jobs.warning_lines(jobs.get(con, jid))
    check("the excess photograph is named in the warnings",
          any("f3.jpg" in w and "limit" in w for w in warns), str(warns))
    check("and it says to upload the rest as a second batch",
          any("second batch" in w for w in warns), str(warns))

    print("\n=== 2. two photographs with the same name (phones do this) ===")
    r = up([("photos", ("IMG_0001.jpg", GOOD, "image/jpeg")),
            ("photos", ("IMG_0001.jpg", shelf_jpeg(1700), "image/jpeg"))])
    check("both are accepted", r.status_code == 303, str(r.status_code))
    jid2 = r.headers["location"].rsplit("/", 1)[-1]
    updir = pathlib.Path(jobs.get(jobs.connect(os.environ["SHELFMARK_DB"]),
                                  jid2)["work_dir"]) / "upload"
    check("neither overwrote the other — two files on disk",
          len(list(updir.glob("*.jpg"))) == 2,
          str([p.name for p in updir.glob("*")]))

    print("\n=== 3. a photograph with no shelf in it fails loudly ===")
    blank = io.BytesIO()
    Image.new("RGB", (1800, 800), (200, 200, 200)).save(blank, "JPEG")
    import shelfcat.spines as _sp
    _real_fake = _sp._FAKE_SPINES
    _sp._FAKE_SPINES = []                     # the model finds nothing
    try:
        r = up([("photos", ("wall.jpg", blank.getvalue(), "image/jpeg"))])
        jid3 = r.headers["location"].rsplit("/", 1)[-1]
        for _ in range(200):
            s = c.get(f"/api/jobs/{jid3}").json()
            if s["state"] in ("done", "failed"):
                break
            time.sleep(0.25)
        check("the job fails rather than reporting 0 volumes as success",
              s["state"] == "failed", f"{s['state']}: {s['message']}")
        check("and the message tells the photographer what to do",
              "no book spines were found" in s["message"], s["message"])
    finally:
        _sp._FAKE_SPINES = _real_fake

    print("\n=== 4. health ===")
    h = c.get("/healthz")
    check("healthz answers", h.status_code == 200, str(h.status_code))
    hj = h.json()
    check("it reports the version", hj.get("version"), str(hj))
    check("it says whether vision is real or fake", hj.get("vision") == "fake",
          str(hj.get("vision")))
    check("it counts active jobs", "active_jobs" in hj, str(hj))

print("\n=== 5. the access code, when set ===")
os.environ["SHELFMARK_ACCESS_CODE"] = "open-sesame"
import importlib
importlib.reload(webapp)
with TestClient(webapp.app) as c:
    r = c.get("/", follow_redirects=False)
    check("the app is gated", r.status_code == 303 and "/unlock" in r.headers["location"],
          f"{r.status_code} {r.headers.get('location')}")
    check("the API returns 401 rather than a redirect",
          c.get("/api/jobs/anything", follow_redirects=False).status_code == 401)
    check("the shell stays open so the icon and CSS still load",
          c.get("/static/app.css").status_code == 200)
    check("and so does the service worker",
          c.get("/sw.js").status_code == 200)
    check("health stays open for the reverse proxy",
          c.get("/healthz").status_code == 200)

    r = c.post("/unlock", data={"code": "wrong", "next": "/"},
               follow_redirects=False)
    check("a wrong code is refused", "bad=1" in r.headers.get("location", ""),
          r.headers.get("location", ""))
    r = c.post("/unlock", data={"code": "open-sesame", "next": "/"},
               follow_redirects=False)
    check("the right code lets you in", r.status_code == 303, str(r.status_code))
    check("the cookie is httponly", "httponly" in r.headers.get("set-cookie", "").lower(),
          r.headers.get("set-cookie", ""))
    check("now the app opens", c.get("/").status_code == 200)

    r = c.post("/unlock", data={"code": "open-sesame", "next": "https://evil.test/x"},
               follow_redirects=False)
    check("an absolute `next` cannot make it an open redirect",
          r.headers["location"] == "/", r.headers["location"])
del os.environ["SHELFMARK_ACCESS_CODE"]

print("\n=== 6. killed mid-job ===")
with tempfile.TemporaryDirectory() as td:
    db = f"{td}/s.db"
    con = jobs.connect(db)
    cid = jobs.create_collection(con, "Interrupted")
    clean = jobs.create(con, "Interrupted", f"{td}/a", collection_id=cid)
    dirty = jobs.create(con, "Interrupted", f"{td}/b", collection_id=cid)
    for j in (clean, dirty):
        jobs._set(con, j, state="running", stage="resolve", progress=0.6)
    con.execute("INSERT OR IGNORE INTO images (sha256, path) VALUES ('s','p')")
    eid = con.execute("INSERT INTO evidence (image_sha, kind, payload, detector) "
                      "VALUES ('s','spine','{}','vlm:fake')").lastrowid
    con.execute("INSERT INTO job_items (job_id, evidence_id) VALUES (?,?)", (dirty, eid))
    con.execute("INSERT INTO records (evidence_id, tier) VALUES (?, 'amber')", (eid,))
    con.commit()
    jobs.recover_stale(con)
    check("a job that wrote nothing is requeued",
          jobs.get(con, clean)["state"] == "queued", jobs.get(con, clean)["state"])
    check("a job that already wrote records is failed, not re-run",
          jobs.get(con, dirty)["state"] == "failed", jobs.get(con, dirty)["state"])
    check("and it says to re-upload that batch",
          "re-upload" in jobs.get(con, dirty)["message"],
          jobs.get(con, dirty)["message"])
    check("the already-recorded volume is not deleted",
          con.execute("SELECT COUNT(*) AS n FROM records").fetchone()["n"] == 1)
    con.close()

print("\n=== 7. the deployment files exist and say the same numbers ===")
root = pathlib.Path(__file__).resolve().parent.parent
unit = (root / "shelfmark.service").read_text()
caddy = (root / "Caddyfile").read_text()
check("there is a systemd unit", "ExecStart" in unit)
check("it keeps the API key out of the unit file", "EnvironmentFile" in unit)
check("it does not hardcode a key", "sk-ant" not in unit)
check("it binds localhost only", "127.0.0.1" in unit)
check("there is a Caddyfile", "reverse_proxy" in caddy)
check("Caddy's body limit matches the app's default total",
      "400MB" in caddy, "must match SHELFMARK_MAX_TOTAL_MB")
check("Caddy health-checks the app", "/healthz" in caddy)
check("`shelfmark` is a real entry point",
      "shelfmark = \"web.cli:main\"" in (root / "pyproject.toml").read_text())

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
_tmp.cleanup()
sys.exit(1 if fails else 0)
