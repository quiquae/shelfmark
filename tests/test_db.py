import sys, tempfile, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shelfcat.db import connect, set_tier

db = os.path.join(tempfile.mkdtemp(), "t.db")
con = connect(db)
con.execute("INSERT INTO images(sha256,path,quality) VALUES('abc','/x.jpg','ok')")
con.execute("INSERT INTO evidence(image_sha,kind,payload,detector,position) "
            "VALUES('abc','spine',?,'vlm:test',3)", (json.dumps({"title_text":"Canterbury Tales"}),))
ev = con.execute("SELECT last_insert_rowid() r").fetchone()["r"]
con.execute("INSERT INTO records(evidence_id,tier,shelf_id,position) VALUES(?,'red','R3B2',3)", (ev,))
con.commit()

ok = []
try:
    set_tier(con, ev, "green")
    ok.append("FAIL: silent promotion was allowed")
except PermissionError as e:
    ok.append("PASS: silent promotion blocked")

ok.append("PASS: reviewed promotion allowed" if set_tier(con, ev, "green", reviewed_by="creagh") == ("red","green") else "FAIL")
ok.append("PASS: demotion needs no review" if set_tier(con, ev, "amber") == ("green","amber") else "FAIL")
try:
    set_tier(con, 999, "green", reviewed_by="x"); ok.append("FAIL: missing record not caught")
except KeyError:
    ok.append("PASS: missing record raises")

for line in ok: print(line)
print("\nrecords:", dict(con.execute("SELECT tier,shelf_id,position FROM records").fetchone()))
