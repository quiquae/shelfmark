"""Offline tests for the web layer, the exporter and the crop rejoin.

Runs with no network: authorities.text_search is replaced by a stub, so this
is safe in a build container -- the condition that left the real HTTP layer
unwritten for a fortnight.

Unlike the older test scripts in this directory, this one exits non-zero when
something fails. `for t in tests/test_*.py; do python $t; done` reports green
on a failing test_stitch.py because those scripts always exit 0.
"""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pymarc import MARCReader

from shelfcat import authorities, export, spines
from web import jobs

fails = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"   {detail}"))
    if not cond:
        fails.append(label)


print("=== 1. export: nothing is dropped ===")
RECS = [
    dict(evidence_id=1, shelf_id="S1-TOP", position=1, title="The Canterbury Tales",
         authors="Geoffrey Chaucer", year="1400", publisher="Caxton",
         isbn13="9780140424386", raw_title="Canterbury Tales", raw_volume=None,
         tier="amber", score=0.95, source="openlibrary", n_reads=2, frames="A,B"),
    dict(evidence_id=2, shelf_id="S1-TOP", position=2, title=None, authors=None,
         year=None, raw_title=None, raw_volume=None, tier="black", score=None,
         source="unresolved", n_reads=1, frames="A"),
    dict(evidence_id=3, shelf_id="S1-TOP", position=3, title="Beowulf",
         authors="Heaney", year="1999", raw_title="Beowulf", raw_volume=None,
         tier="amber", score=0.70, source="openlibrary", n_reads=1, frames="B"),
]
with tempfile.TemporaryDirectory() as td:
    td = pathlib.Path(td)
    csv_stats = export.to_csv(td / "c.csv", RECS)
    marc_stats = export.to_marc(td / "c.mrc", RECS)
    check("every volume gets a CSV row", csv_stats["rows"] == 3, str(csv_stats))
    check("every volume gets a MARC record", marc_stats["records"] == 3, str(marc_stats))
    recs = list(MARCReader((td / "c.mrc").open("rb")))
    check("MARC re-reads as 3 records", len(recs) == 3, str(len(recs)))

    check("unresolved still has a 245",
          recs[1]["245"]["a"] == "[Spine not legible]", recs[1]["245"]["a"])
    check("low score is flagged for review",
          export.needs_review(RECS[2]) is True, str(RECS[2]["score"]))
    check("good score is not flagged",
          export.needs_review(RECS[0]) is False, str(RECS[0]["score"]))
    check("unresolved is flagged", export.needs_review(RECS[1]) is True)
    check("2 of 3 need review", marc_stats["needs_review"] == 2,
          str(marc_stats["needs_review"]))

    print("\n=== 2. MARC: Koha finds the book ===")
    for i, r in enumerate(recs):
        f = r["952"]
        check(f"record {i+1} has 952 with a call number",
              f is not None and f.get("o") == f"S1-TOP/{i+1:03d}",
              str(f.get("o") if f else None))
    check("852 carries the same call number for non-Koha systems",
          recs[0]["852"]["h"] == "S1-TOP/001", recs[0]["852"]["h"])
    check("008 is exactly 40 characters", len(recs[0]["008"].data) == 40,
          f"{len(recs[0]['008'].data)}")
    check("review note travels in 952$z",
          bool(recs[1]["952"].get("z")), str(recs[1]["952"].get("z")))
    check("provenance is in 500",
          "shelf photograph" in recs[0]["500"]["a"], recs[0]["500"]["a"][:40])
    # pymarc's Record.__getitem__ raises KeyError for an absent field rather
    # than returning None, so absence is asserted with get_fields().
    check("no ISBN invented for an unread spine",
          recs[1].get_fields("020") == [], str(recs[1].get_fields("020")))
    check("a real ISBN is carried through",
          recs[0]["020"]["a"] == "9780140424386", recs[0]["020"]["a"])

print("\n=== 3. crops are rejoined, not counted twice ===")
t = spines.transcribe_frame("IMG_1", ["p1.jpg", "p2.jpg"], fake=True)
check("two crops of 3 spines collapse to 3", len(t["spines"]) == 3,
      str(len(t["spines"])))
check("the seam records the overlap it found",
      t["crop_seams"] and t["crop_seams"][0]["overlap"] == 3, str(t["crop_seams"]))
check("positions are renumbered 1..n",
      [s["i"] for s in t["spines"]] == [1, 2, 3], str([s["i"] for s in t["spines"]]))
check("the illegible spine survives the rejoin",
      any(s["legibility"] == "illegible" for s in t["spines"]))
single = spines.transcribe_frame("IMG_1", ["p1.jpg"], fake=True)
check("one crop needs no seam", single["crop_seams"] == [], str(single["crop_seams"]))

print("\n=== 4. read_spine refuses to guess ===")
try:
    spines.read_spine("x.jpg", fake=False)
    check("real vision path raises", False, "it returned instead")
except NotImplementedError:
    check("real vision path raises NotImplementedError", True)

print("\n=== 5. the resolver's author is a score, not a filter ===")
check("an author that matches nothing must not zero the query",
      "author" not in authorities._openlibrary_text.__doc__.lower()
      or "constraint" in authorities._openlibrary_text.__doc__.lower()
      or "filter" in authorities._openlibrary_text.__doc__.lower())
check("contradicted author caps the score, it does not hide the match",
      authorities.score_match("Beowulf", "Heaney", "Beowulf", "Tolkien") < 0.6,
      str(authorities.score_match("Beowulf", "Heaney", "Beowulf", "Tolkien")))
check("a close second candidate downgrades the tier",
      authorities.tier_for([0.97, 0.94]) == "red",
      authorities.tier_for([0.97, 0.94]))
check("nothing found is black", authorities.tier_for([]) == "black")

print("\n=== 6. breaker stops hammering a throttled authority ===")
authorities.reset_breaker()
check("breaker starts clear", authorities.breaker_state() == {})
authorities._FAILS["googlebooks"] = authorities.BREAKER_TRIP
check("breaker trips at the threshold",
      "googlebooks" in authorities.breaker_state(),
      str(authorities.breaker_state()))
authorities.reset_breaker()

print("\n=== 7. a job runs end to end with no network ===")
# The vision flag is process-wide and read at call time, which is what makes
# the pipeline exercisable before the real CV exists. The job records which
# backend ran in evidence.detector, so a fake run can never be mistaken for
# a real one after the fact.
import os
_prev_fake = os.environ.get(spines.FAKE_VISION_ENV)
os.environ[spines.FAKE_VISION_ENV] = "1"
_real = authorities.resolve
authorities.resolve = lambda title, author=None, isbn=None, limit=5: (
    [{"authority": "stub", "authority_id": "x1", "title": title,
      "authors": author, "year": "1900", "publisher": "P", "isbn13": None,
      "score": 0.95, "raw": {}}], "amber", "text")
try:
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        work = td / "job"
        (work / "upload").mkdir(parents=True)
        # a 2000px frame of vertical bars: enough for the band finder
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (2400, 1100), (40, 30, 24))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 200, 2400, 900], fill=(24, 20, 18))
        x = 100
        while x < 2100:
            d.rectangle([x, 210, x + 70, 890],
                        fill=(90 + x % 120, 70, 60), outline=(10, 10, 10), width=3)
            for k in range(5):
                d.rectangle([x + 12, 260 + k * 40, x + 58, 268 + k * 40], fill=(230, 225, 215))
            x += 78
        im.save(work / "upload" / "IMG_5001.jpg", "JPEG", quality=93)
        (work / "upload" / "readme.txt").write_text("not an image")

        dbp = str(td / "t.db")
        con = jobs.connect(dbp)
        jid = jobs.create(con, "Test", work)
        con.close()
        jobs.run_once(dbp)

        con = jobs.connect(dbp)
        j = jobs.get(con, jid)
        check("job reached done", j["state"] == "done",
              f"{j['state']}: {j['message']}")
        check("a frame was ingested", j["n_frames"] == 1, str(j["n_frames"]))
        check("volumes were recorded", j["n_books"] > 0, str(j["n_books"]))
        recs = jobs.records_for(con, jid)
        check("records_for returns every volume",
              len(recs) > 0 and len(recs) == j["n_books"],
              f"{len(recs)} vs {j['n_books']}")
        check("shelf positions are contiguous from 1",
              len(recs) > 0
              and [r["position"] for r in recs] == list(range(1, len(recs) + 1)),
              str([r["position"] for r in recs]))
        check("the fake backend is recorded as fake",
              all(r["detector"] == "vlm:fake" for r in recs) and len(recs) > 0,
              str({r["detector"] for r in recs}))
        check("an unreadable spine still reaches the records",
              any(r["source"] == "unresolved" or not r["raw_title"] for r in recs),
              str([r["raw_title"] for r in recs]))
        lines = jobs.warning_lines(j)
        check("the non-image file was reported, not silently dropped",
              any("readme.txt" in w for w in lines), str(lines))
        check("no warning renders as a raw dict",
              all(not w.strip().startswith("{") for w in lines), str(lines))
        check("catalogue.mrc was written",
              (work / "out" / "catalogue.mrc").exists())
        check("catalogue.csv was written",
              (work / "out" / "catalogue.csv").exists())
        mrc = work / "out" / "catalogue.mrc"
        if mrc.exists():
            n_marc = len(list(MARCReader(mrc.open("rb"))))
            check("MARC record count equals volume count",
                  n_marc == len(recs) and n_marc > 0, f"{n_marc} vs {len(recs)}")
        else:
            check("MARC record count equals volume count", False, "no .mrc written")
        con.close()
finally:
    authorities.resolve = _real
    if _prev_fake is None:
        os.environ.pop(spines.FAKE_VISION_ENV, None)
    else:
        os.environ[spines.FAKE_VISION_ENV] = _prev_fake

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
