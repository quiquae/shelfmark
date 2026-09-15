"""Collections: two sittings, continuous shelf numbering, one export.

No network, no server. Exits non-zero on failure.
"""
import os, pathlib, sys, tempfile, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
_tmp = tempfile.TemporaryDirectory()
TMP = pathlib.Path(_tmp.name)
os.environ["SHELFMARK_WORK"] = str(TMP / "jobs")
os.environ["SHELFMARK_DB"] = str(TMP / "c.db")
os.environ["SHELFCAT_FAKE_VISION"] = "1"

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from pymarc import MARCReader

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
      "ddc": "823", "lcc": None, "subjects": ["Fiction"], "raw": {}}],
    "amber", "text")


def frame(path, seed):
    im = Image.new("RGB", (2400, 1100), (40, 30, 24)); d = ImageDraw.Draw(im)
    d.rectangle([0, 200, 2400, 900], fill=(24, 20, 18)); x = 100
    while x < 2100:
        d.rectangle([x, 210, x + 70, 890], fill=(90 + (x + seed) % 120, 70, 60),
                    outline=(10, 10, 10), width=3)
        for k in range(5):
            d.rectangle([x + 12, 260 + k * 40, x + 58, 268 + k * 40], fill=(235, 230, 220))
        x += 78
    im.save(path, "JPEG", quality=93)


src = TMP / "src"; src.mkdir()
for i in range(4):
    frame(src / f"IMG_60{i:02d}.jpg", seed=i * 23)


def upload(client, names, collection=None, cid=None):
    files = [("photos", (n, (src / n).open("rb"), "image/jpeg")) for n in names]
    data = {"collection_id": cid} if cid else {"collection": collection}
    r = client.post("/upload", data=data, files=files, follow_redirects=False)
    jid = r.headers["location"].rsplit("/", 1)[-1]
    for _ in range(200):
        s = client.get(f"/api/jobs/{jid}").json()
        if s["state"] in ("done", "failed"):
            break
        time.sleep(0.25)
    return jid, s


with TestClient(webapp.app) as client:
    print("=== 1. first sitting creates a collection ===")
    j1, s1 = upload(client, ["IMG_6000.jpg", "IMG_6001.jpg"], collection="Main library")
    check("the first batch completed", s1["state"] == "done", str(s1.get("message")))
    con = jobs.connect(os.environ["SHELFMARK_DB"])
    colls = jobs.collections(con)
    check("a collection was created", len(colls) == 1, str(len(colls)))
    cid = colls[0]["id"]
    check("it is named from the form", colls[0]["name"] == "Main library", colls[0]["name"])
    job1 = jobs.get(con, j1)
    check("the batch is attached to it", job1["collection_id"] == cid)
    check("its layer count was recorded", (job1["n_layers"] or 0) > 0, str(job1["n_layers"]))
    shelves1 = [r["shelf_id"] for r in jobs.records_for(con, j1)]
    off = jobs.layer_offset(con, cid)
    check("layer_offset reflects the first batch", off == job1["n_layers"],
          f"{off} vs {job1['n_layers']}")

    print("\n=== 2. a second sitting continues the shelf numbering ===")
    j2, s2 = upload(client, ["IMG_6002.jpg", "IMG_6003.jpg"], cid=cid)
    check("the second batch completed", s2["state"] == "done", str(s2.get("message")))
    con = jobs.connect(os.environ["SHELFMARK_DB"])
    shelves2 = [r["shelf_id"] for r in jobs.records_for(con, j2)]
    check("the second batch did NOT restart at shelf 1 row top",
          not (set(shelves1) & set(shelves2)),
          f"{sorted(set(shelves1))} vs {sorted(set(shelves2))}")
    cn1 = {r["shelf_id"] + "/" + str(r["position"]) for r in jobs.records_for(con, j1)}
    cn2 = {r["shelf_id"] + "/" + str(r["position"]) for r in jobs.records_for(con, j2)}
    check("no call number collides between sittings", not (cn1 & cn2), str(cn1 & cn2))
    check("a warning records the continuation",
          any("continued from layer" in w for w in jobs.warning_lines(jobs.get(con, j2))),
          str(jobs.warning_lines(jobs.get(con, j2))))

    print("\n=== 3. the collection is one catalogue ===")
    all_recs = jobs.records_for(con, collection_id=cid)
    per_job = len(jobs.records_for(con, j1)) + len(jobs.records_for(con, j2))
    check("collection scope returns both sittings", len(all_recs) == per_job,
          f"{len(all_recs)} vs {per_job}")
    check("it is in walking order",
          [jobs.shelf_sort_key(r) for r in all_recs] ==
          sorted(jobs.shelf_sort_key(r) for r in all_recs))
    page = client.get(f"/collection/{cid}")
    check("the collection page renders", page.status_code == 200, str(page.status_code))
    check("it offers to add more shelves", 'name="collection_id"' in page.text)
    check("it says where numbering will continue from", "continues from row" in page.text)

    print("\n=== 4. one export for the whole collection ===")
    mrc = client.get(f"/collection/{cid}/download/mrc")
    check("collection MARC downloads", mrc.status_code == 200, str(mrc.status_code))
    out = TMP / "coll.mrc"; out.write_bytes(mrc.content)
    recs = list(MARCReader(out.open("rb")))
    check("it holds every volume from both sittings", len(recs) == len(all_recs),
          f"{len(recs)} vs {len(all_recs)}")
    calls = [r["952"]["o"] for r in recs if r["952"]]
    check("every record has a call number", len(calls) == len(recs))
    check("call numbers are unique across the collection",
          len(set(calls)) == len(calls), f"{len(calls) - len(set(calls))} duplicated")
    check("the CSV covers the collection too",
          client.get(f"/collection/{cid}/download/csv").status_code == 200)
    check("the workbook is refused at collection scope, with a reason",
          client.get(f"/collection/{cid}/download/xlsx").status_code == 409)

    print("\n=== 5. review resumes across sittings ===")
    q = jobs.review_queue(con, collection_id=cid)
    check("the queue spans both sittings",
          len({x["job_id"] for x in q}) == 2 if len(q) > 1 else True,
          str({x["job_id"] for x in q}))
    r = client.get(f"/collection/{cid}/review", follow_redirects=False)
    check("resume redirects into a specific volume",
          r.status_code == 303 and "/review/" in r.headers.get("location", ""),
          f"{r.status_code} {r.headers.get('location')}")
    if q:
        first = q[0]
        page = client.get(f"/job/{first['job_id']}/review/{first['evidence_id']}")
        check("the review screen counts the whole collection",
              f"of {len(q)}" in page.text, str(len(q)))
        # settle everything, from whichever batch it belongs to
        guard = 0
        while guard < 400:
            qq = jobs.review_queue(jobs.connect(os.environ["SHELFMARK_DB"]),
                                   collection_id=cid)
            if not qq:
                break
            client.post(f"/api/job/{qq[0]['job_id']}/review/{qq[0]['evidence_id']}",
                        json={"action": "skip", "reviewer": "creagh"})
            guard += 1
        check("the collection queue drains", not qq, str(len(qq)))
        r = client.get(f"/collection/{cid}/review", follow_redirects=False)
        check("an empty queue returns to the collection",
              r.status_code == 303 and "/collection/" in r.headers["location"],
              r.headers.get("location", ""))

    print("\n=== 6. shelf work is collection-wide ===")
    w = jobs.shelf_work(con, collection_id=cid)
    check("it covers both sittings", w["total"] == len(all_recs),
          f"{w['total']} vs {len(all_recs)}")
    check("the collection worklist renders",
          client.get(f"/collection/{cid}/worklist").status_code == 200)
    check("and downloads as CSV",
          client.get(f"/collection/{cid}/worklist.csv").status_code == 200)
    check("the index lists the collection",
          "Main library" in client.get("/").text)
    con.close()

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
_tmp.cleanup()
sys.exit(1 if fails else 0)
