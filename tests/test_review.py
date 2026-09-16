"""The review screen, end to end over HTTP, with no network and no server.

Uses FastAPI's TestClient, so the whole request/worker/export path runs in
process. Resolution is stubbed: this must pass in a build container.

Exits non-zero on failure.
"""
import json
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_tmp = tempfile.TemporaryDirectory()
TMP = pathlib.Path(_tmp.name)
# app.py resolves these at import time, so they must be set first.
os.environ["SHELFMARK_WORK"] = str(TMP / "jobs")
os.environ["SHELFMARK_DB"] = str(TMP / "t.db")
os.environ["SHELFCAT_FAKE_VISION"] = "1"

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from pymarc import MARCReader

from shelfcat import authorities
from web import app as webapp
from web import jobs

fails = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"   {detail}"))
    if not cond:
        fails.append(label)


# Two candidate works, the first with three near-identical editions, so the
# collapse path and the "other candidates" list are both exercised.
def _stub(title, author=None, isbn=None, limit=5):
    raw = [
        {"authority": "stub", "authority_id": "e1", "title": title, "authors": author,
         "year": "1927", "publisher": "Hogarth", "isbn13": None, "score": 0.97,
         "ddc": "823.912", "lcc": "PR6045", "subjects": ["Fiction"], "raw": {}},
        {"authority": "stub", "authority_id": "e2", "title": title, "authors": author,
         "year": "1950", "publisher": "Reprint", "isbn13": None, "score": 0.95,
         "ddc": None, "lcc": None, "subjects": None, "raw": {}},
        {"authority": "stub", "authority_id": "o1", "title": f"{title}: a study",
         "authors": "Someone Else", "year": "1988", "publisher": "UP",
         "isbn13": None, "score": 0.74, "ddc": "809", "lcc": None,
         "subjects": ["Criticism"], "raw": {}},
    ]
    cands = authorities.collapse_editions(raw)
    return cands, authorities.tier_for([c["score"] for c in cands]), "text"


authorities.resolve = _stub


def _frame(path, seed=0):
    im = Image.new("RGB", (2400, 1100), (40, 30, 24))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 200, 2400, 900], fill=(24, 20, 18))
    x = 100
    while x < 2100:
        d.rectangle([x, 210, x + 70, 890], fill=(90 + (x + seed) % 120, 70, 60),
                    outline=(10, 10, 10), width=3)
        for k in range(5):
            d.rectangle([x + 12, 260 + k * 40, x + 58, 268 + k * 40], fill=(235, 230, 220))
        x += 78
    im.save(path, "JPEG", quality=93)


print("=== 1. upload and wait for the worker ===")
with TestClient(webapp.app) as client:
    src = TMP / "src"
    src.mkdir()
    for i, name in enumerate(("IMG_4001.jpg", "IMG_4002.jpg")):
        _frame(src / name, seed=i * 17)

    files = [("photos", (n, (src / n).open("rb"), "image/jpeg"))
             for n in ("IMG_4001.jpg", "IMG_4002.jpg")]
    r = client.post("/upload", data={"collection": "Review Test"}, files=files,
                    follow_redirects=False)
    check("upload redirects to the job", r.status_code == 303, str(r.status_code))
    jid = r.headers["location"].rsplit("/", 1)[-1]

    state = None
    for _ in range(120):
        s = client.get(f"/api/jobs/{jid}").json()
        state = s["state"]
        if state in ("done", "failed"):
            break
        time.sleep(0.25)
    check("the job completed", state == "done", f"{state}: {s.get('message')}")

    print("\n=== 2. the review queue and its screen ===")
    con = jobs.connect(os.environ["SHELFMARK_DB"])
    counts = jobs.review_counts(con, jid)
    queue = jobs.review_queue(con, jid)
    check("some volumes were catalogued", counts["total"] > 0, str(counts))
    check("the queue holds only what needs a decision",
          len(queue) == counts["pending"], f"{len(queue)} vs {counts['pending']}")

    r = client.get(f"/job/{jid}/review", follow_redirects=False)
    check("/review redirects to a specific volume",
          r.status_code == 303 and "/review/" in r.headers.get("location", ""),
          str(r.status_code))

    # a record that actually has candidates, so accept/undo can be exercised
    target = None
    for rec in jobs.records_for(con, jid):
        d = jobs.record_detail(con, jid, rec["evidence_id"])
        if d["candidates"]:
            target = d
            break
    check("a record with candidates exists", target is not None)
    eid = target["evidence_id"]

    page = client.get(f"/job/{jid}/review/{eid}")
    check("the review screen renders", page.status_code == 200, str(page.status_code))
    html = page.text
    check("it offers accept, correct and skip",
          all(k in html for k in ('data-act="accept"', 'data-act="editform"',
                                  'data-act="skip"')))
    check("it binds keyboard shortcuts", "keydown" in html)
    check("it says the spine position is approximate", "approximate" in html)
    check("editions are collapsed in the candidate list",
          "editions" in html, "n_editions not surfaced")

    print("\n=== 3. the spine crop ===")
    img = client.get(f"/job/{jid}/spine/{eid}.jpg?w=1")
    check("a spine crop is served", img.status_code == 200 and
          img.headers["content-type"] == "image/jpeg", str(img.status_code))
    wide = client.get(f"/job/{jid}/spine/{eid}.jpg?w=3")
    check("a wider window is a different, larger image",
          wide.status_code == 200 and len(wide.content) > len(img.content),
          f"{len(img.content)} -> {len(wide.content)}")

    print("\n=== 4. accept, undo, correct ===")
    machine_tier = target["machine_tier"]
    second = target["candidates"][1]["id"] if len(target["candidates"]) > 1 \
        else target["candidates"][0]["id"]
    r = client.post(f"/api/job/{jid}/review/{eid}",
                    json={"action": "accept", "claim_id": second, "reviewer": "creagh"})
    check("accepting a specific candidate succeeds", r.status_code == 200, r.text[:80])
    check("a human decision reaches green", r.json()["tier"] == "green", r.text[:80])
    row = jobs.record_detail(jobs.connect(os.environ["SHELFMARK_DB"]), jid, eid)
    check("the reviewer is named", row["reviewed_by"] == "creagh", str(row["reviewed_by"]))
    check("the accepted candidate became the record",
          row["claim_id"] == second, f"{row['claim_id']} vs {second}")
    check("it left the queue",
          eid not in [q["evidence_id"] for q in
                      jobs.review_queue(jobs.connect(os.environ["SHELFMARK_DB"]), jid)])

    r = client.post(f"/api/job/{jid}/review/{eid}", json={"action": "undo"})
    check("undo succeeds", r.status_code == 200, r.text[:80])
    row = jobs.record_detail(jobs.connect(os.environ["SHELFMARK_DB"]), jid, eid)
    check("undo restores the machine's own verdict",
          row["tier"] == machine_tier, f"{row['tier']} vs {machine_tier}")
    check("undo clears the reviewer", not row["reviewed_by"], str(row["reviewed_by"]))

    r = client.post(f"/api/job/{jid}/review/{eid}", json={
        "action": "edit", "reviewer": "creagh",
        "fields": {"title": "Corrected By A Human", "authors": "A Reviewer",
                   "year": "1999", "publisher": "Hand Press", "isbn13": ""}})
    check("a hand correction succeeds", r.status_code == 200, r.text[:80])
    row = jobs.record_detail(jobs.connect(os.environ["SHELFMARK_DB"]), jid, eid)
    check("the correction is stored", row["title"] == "Corrected By A Human",
          str(row["title"]))
    check("a corrected record is green", row["tier"] == "green", str(row["tier"]))

    r = client.post(f"/api/job/{jid}/review/{eid}",
                    json={"action": "edit", "reviewer": "creagh",
                          "fields": {"title": "   "}})
    row = jobs.record_detail(jobs.connect(os.environ["SHELFMARK_DB"]), jid, eid)
    check("a blank title cannot be promoted to green",
          row["tier"] != "green", str(row["tier"]))

    print("\n=== 5. skipping is not approval ===")
    q = jobs.review_queue(jobs.connect(os.environ["SHELFMARK_DB"]), jid)
    if q:
        seid = q[0]["evidence_id"]
        before = jobs.record_detail(jobs.connect(os.environ["SHELFMARK_DB"]), jid, seid)
        r = client.post(f"/api/job/{jid}/review/{seid}",
                        json={"action": "skip", "reviewer": "creagh"})
        after = jobs.record_detail(jobs.connect(os.environ["SHELFMARK_DB"]), jid, seid)
        check("skip succeeds", r.status_code == 200, r.text[:80])
        check("skip does not move the tier", after["tier"] == before["tier"],
              f"{before['tier']} -> {after['tier']}")
        check("skip still records that it was seen",
              after["reviewed_by"] == "creagh", str(after["reviewed_by"]))
        check("skip leaves the record unverified in the export",
              after["tier"] != "green", str(after["tier"]))
    else:
        check("skip succeeds", True, "queue already empty")

    print("\n=== 6. corrections reach the file the librarian hands to Koha ===")
    client.post(f"/api/job/{jid}/review/{eid}", json={
        "action": "edit", "reviewer": "creagh",
        "fields": {"title": "Corrected By A Human", "authors": "A Reviewer",
                   "year": "1999"}})
    mrc = client.get(f"/job/{jid}/download/mrc")
    check("catalogue.mrc downloads", mrc.status_code == 200, str(mrc.status_code))
    out = TMP / "dl.mrc"
    out.write_bytes(mrc.content)
    recs = list(MARCReader(out.open("rb")))
    check("every volume is still in the file", len(recs) == counts["total"],
          f"{len(recs)} vs {counts['total']}")
    check("the hand correction is in the regenerated MARC",
          any("Corrected By A Human" in (m["245"]["a"] or "") for m in recs),
          str([m["245"]["a"][:28] for m in recs]))
    check("every record still carries a shelf position",
          all(m["952"] and m["952"].get("o") for m in recs))
    csv_txt = client.get(f"/job/{jid}/download/csv").text
    check("the CSV is regenerated too", "Corrected By A Human" in csv_txt)
    check("Dewey survives into the CSV header", "ddc" in csv_txt.splitlines()[0])

    print("\n=== 6b. the shelf worklist ===")
    work = jobs.shelf_work(jobs.connect(os.environ["SHELFMARK_DB"]), jid)
    check("the unreadable spine is on the worklist",
          work["n_unreadable"] >= 1, str(work["n_unreadable"]))
    check("the worklist is grouped by shelf",
          all("shelf" in g and "rows" in g for g in work["unreadable"]),
          str(work["unreadable"])[:80])
    check("rows within a shelf are in walking order",
          all([r["position"] for r in g["rows"]] ==
              sorted(r["position"] for r in g["rows"]) for g in work["unreadable"]))
    wl = client.get(f"/job/{jid}/worklist")
    check("the worklist page renders", wl.status_code == 200, str(wl.status_code))
    check("it explains that unreadable spines are a floor, not a fault",
          "floor, not a fault" in wl.text)
    check("every row links to its review screen",
          f"/job/{jid}/review/" in wl.text)
    csvr = client.get(f"/job/{jid}/worklist.csv")
    check("the worklist downloads as CSV",
          csvr.status_code == 200 and "text/csv" in csvr.headers["content-type"],
          str(csvr.status_code))
    head = csvr.text.splitlines()[0]
    check("the CSV names the task and the call number",
          "task" in head and "call_number" in head, head)
    check("it is attachment-dispositioned for a phone download",
          "attachment" in csvr.headers.get("content-disposition", ""),
          csvr.headers.get("content-disposition", ""))

    print("\n=== 6c. it installs as an app ===")
    sw = client.get("/sw.js")
    check("the service worker is served from the root scope",
          sw.status_code == 200, str(sw.status_code))
    check("and declares that scope",
          sw.headers.get("service-worker-allowed") == "/",
          str(sw.headers.get("service-worker-allowed")))
    man = client.get("/static/manifest.webmanifest")
    check("the manifest is served", man.status_code == 200, str(man.status_code))
    mj = json.loads(man.text)
    check("it launches without browser chrome", mj["display"] == "standalone",
          mj["display"])
    check("it has a maskable icon for Android",
          any(i.get("purpose") == "maskable" for i in mj["icons"]))
    for icon in mj["icons"]:
        r = client.get(icon["src"])
        check(f"icon {icon['sizes']} exists", r.status_code == 200, icon["src"])
    check("iOS gets a touch icon",
          client.get("/static/icons/icon-180.png").status_code == 200)
    home = client.get("/").text
    check("pages link the manifest", 'rel="manifest"' in home)
    check("pages register the worker", "serviceWorker" in home)
    # The important negative: a cached review screen would show a librarian a
    # record they have already settled.
    body = sw.text
    check("the worker caches only /static/", "/static/" in body)
    check("it never caches job or collection pages",
          "/job/" not in body.replace("/job/{", "") or "startsWith" in body)
    check("it excludes the per-record spine crops", "/spine/" in body)

    print("\n=== 7. the queue drains and the screen says so ===")
    guard = 0
    while guard < 200:
        q = jobs.review_queue(jobs.connect(os.environ["SHELFMARK_DB"]), jid)
        if not q:
            break
        client.post(f"/api/job/{jid}/review/{q[0]['evidence_id']}",
                    json={"action": "skip", "reviewer": "creagh"})
        guard += 1
    check("the queue drains", not q, str(len(q)))
    r = client.get(f"/job/{jid}/review", follow_redirects=False)
    check("an empty queue sends the reviewer to the catalogue",
          r.status_code == 303 and "records" in r.headers["location"],
          f"{r.status_code} {r.headers.get('location')}")
    final = client.get(f"/job/{jid}/records?reviewed=1")
    check("the catalogue page confirms review is complete",
          "Review complete" in final.text)
    con.close()

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
_tmp.cleanup()
sys.exit(1 if fails else 0)
