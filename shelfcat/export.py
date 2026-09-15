"""Interchange export: CSV and MARC21.

Two rules govern this module.

**Nothing is dropped.** A spine that could not be read, or that no authority
matched, still produces a row and still produces a MARC record. It carries
`source="unresolved"`, the raw transcription in `raw_title`, and its shelf
position. A librarian rejects a tool that silently loses volumes, and an
inventory with a known hole is worth more than one with an unknown hole.

**The shelf position is the point.** A catalogue that cannot lead you back to
the physical book is a counting exercise. Koha keeps item-level data in its
local-use field **952**, not in 852 -- only a few 852 subfields are free in
MARC21, so Koha substituted a local field for the columns its items table
needs. Shelf order written only to 852 imports as bibliographic holdings text
and never becomes findable item data. So every record gets both: 952 for Koha
(and Evergreen, which reads it on import), 852 for anything else.
"""
import csv
import datetime
import pathlib

from pymarc import Field, MARCWriter, Record, Subfield

# Below this score the match is a hypothesis, not a fact, and a human must see
# it before the record is trusted. Calibrate against ground truth before
# moving it -- see the note in README.
REVIEW_BELOW = 0.82

CSV_COLUMNS = [
    "shelf_id", "position", "title", "authors", "year", "publisher", "isbn13",
    "ddc", "lcc", "subjects",
    "raw_title", "raw_author", "raw_volume", "tier", "confidence", "score",
    "source", "needs_review", "n_reads", "frames", "note",
]


def _clean(v):
    return "" if v is None else str(v)


def needs_review(rec) -> bool:
    """Anything a machine is not entitled to assert on its own."""
    if rec.get("source") == "unresolved":
        return True
    if rec.get("tier") in ("red", "black"):
        return True
    score = rec.get("score")
    return score is None or float(score) < REVIEW_BELOW


def to_csv(path, records):
    """One row per physical volume seen, in shelf order. Never fewer rows than
    volumes."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in records:
            row = {k: _clean(r.get(k)) for k in CSV_COLUMNS}
            subj = r.get("subjects")
            row["subjects"] = "; ".join(subj) if isinstance(subj, list) else _clean(subj)
            row["needs_review"] = "yes" if needs_review(r) else "no"
            w.writerow(row)
    return {"path": str(path), "rows": len(records)}


def call_number(rec) -> str:
    """Shelf order, human-readable and sortable. This is what gets the book
    back off the shelf, so it is built even when nothing else resolved."""
    shelf = _clean(rec.get("shelf_id")) or "UNSHELVED"
    pos = rec.get("position")
    return f"{shelf}/{int(pos):03d}" if pos is not None else shelf


def _leader_and_008(rec):
    """MARC21 fixed fields for a monograph.

    008 is 40 characters and positional; a short or mis-padded 008 is a
    classic silent import failure, so it is built by explicit slices."""
    year = _clean(rec.get("year")).strip()
    y = year[:4] if len(year) >= 4 and year[:4].isdigit() else None
    entered = datetime.date.today().strftime("%y%m%d")      # 00-05
    f008 = (
        entered
        + ("s" if y else "n")        # 06 type of date
        + (y if y else "uuuu")       # 07-10 date 1
        + "    "                     # 11-14 date 2
        + "xx "                      # 15-17 place, unknown
        + " " * 17                   # 18-34 material specific, unstated
        + "   "                      # 35-37 language, unstated
        + " "                        # 38 modified record
        + "d"                        # 39 cataloguing source: other
    )
    assert len(f008) == 40, f"008 is {len(f008)} chars, must be 40"
    return f008


def _record(rec, org="ShelfMark", library="MAIN"):
    r = Record(force_utf8=True)
    # 05 status n=new, 06 type a=language material, 07 level m=monograph,
    # 09 a=UCS/Unicode, 17 encoding level 7=minimal, 18 u=unknown rules
    r.leader = "00000nam a22000007u 4500"

    ident = _clean(rec.get("record_id")) or f"SM{rec.get('evidence_id', 0):08d}"
    r.add_field(Field(tag="001", data=ident))
    r.add_field(Field(tag="003", data=org))
    r.add_field(Field(tag="008", data=_leader_and_008(rec)))

    if rec.get("isbn13"):
        r.add_field(Field(tag="020", indicators=[" ", " "],
                          subfields=[Subfield("a", _clean(rec["isbn13"]))]))

    r.add_field(Field(tag="040", indicators=[" ", " "],
                      subfields=[Subfield("a", org), Subfield("b", "eng"),
                                 Subfield("c", org)]))

    # Classification travels as its own fields, never folded into the call
    # number: 082 is what a Dewey library reclassifies from, and it is
    # authority data. 55% of real spines resolve to a Dewey number and 84% to
    # an LC class, so most records carry at least one.
    if rec.get("ddc"):
        r.add_field(Field(tag="082", indicators=["0", "4"],
                          subfields=[Subfield("a", _clean(rec["ddc"])),
                                     Subfield("2", "23")]))
    if rec.get("lcc"):
        r.add_field(Field(tag="050", indicators=[" ", "4"],
                          subfields=[Subfield("a", _clean(rec["lcc"]))]))

    authors = _clean(rec.get("authors")).strip()
    if authors:
        r.add_field(Field(tag="100", indicators=["1", " "],
                          subfields=[Subfield("a", authors)]))

    # 245: a title is mandatory. An unread spine gets an explicit
    # bracketed placeholder rather than an empty field, because a MARC record
    # with no 245 is rejected outright and the volume would vanish.
    title = _clean(rec.get("title")).strip() or _clean(rec.get("raw_title")).strip()
    if not title:
        title = "[Spine not legible]"
    ind1 = "1" if authors else "0"
    subs = [Subfield("a", title)]
    if authors:
        subs.append(Subfield("c", authors + "."))
    vol = _clean(rec.get("raw_volume")).strip()
    if vol:
        subs.append(Subfield("n", vol))
    r.add_field(Field(tag="245", indicators=[ind1, "0"], subfields=subs))

    pub, year = _clean(rec.get("publisher")).strip(), _clean(rec.get("year")).strip()
    if pub or year:
        subs = []
        if pub:
            subs.append(Subfield("b", pub))
        if year:
            subs.append(Subfield("c", year))
        r.add_field(Field(tag="264", indicators=[" ", "1"], subfields=subs))

    # The audit trail travels with the record. A cataloguer who cannot see
    # where a field came from cannot correct it.
    prov = [f"Catalogued from a shelf photograph by shelfmark."]
    if rec.get("frames"):
        prov.append(f"Source frame(s): {_clean(rec['frames'])}.")
    if rec.get("n_reads"):
        prov.append(f"Spine read {rec['n_reads']}x independently.")
    if rec.get("raw_title") and rec.get("raw_title") != rec.get("title"):
        prov.append(f'Spine reads: "{_clean(rec["raw_title"])}".')
    if rec.get("score") is not None:
        prov.append(f"Match score {float(rec['score']):.2f} ({_clean(rec.get('source'))}).")
    if needs_review(rec):
        prov.append("UNVERIFIED - requires review before use.")
    r.add_field(Field(tag="500", indicators=[" ", " "],
                      subfields=[Subfield("a", " ".join(prov))]))

    for term in (rec.get("subjects") or [])[:6]:
        t = _clean(term).strip()
        if t:
            r.add_field(Field(tag="650", indicators=[" ", "4"],
                              subfields=[Subfield("a", t)]))

    cn = call_number(rec)
    # 852 for portability (Evergreen, FOLIO, any MARC holdings consumer)
    r.add_field(Field(tag="852", indicators=["8", " "],
                      subfields=[Subfield("a", org), Subfield("b", library),
                                 Subfield("h", cn)]))
    # 952 is what makes Koha create a findable item
    item = [Subfield("a", library), Subfield("b", library),
            Subfield("o", cn), Subfield("y", "BK")]
    if needs_review(rec):
        item.append(Subfield("z", "Needs review: machine-catalogued from spine photo"))
    r.add_field(Field(tag="952", indicators=[" ", " "], subfields=item))

    return r


def to_marc(path, records, org="ShelfMark", library="MAIN"):
    """Write catalogue.mrc. One record per volume, including the unreadable."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n_review = 0
    with path.open("wb") as f:
        writer = MARCWriter(f)
        try:
            for rec in records:
                if needs_review(rec):
                    n_review += 1
                writer.write(_record(rec, org=org, library=library))
        finally:
            writer.close()
    return {"path": str(path), "records": len(records), "needs_review": n_review}
