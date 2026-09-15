# shelfmark

**Photograph a shelf. Get an ordered catalogue.**

Point a phone at a shelf of books, upload the photographs, and download a
MARC21 file that Koha, Evergreen or FOLIO will import — with every volume's
shelf position preserved, so the catalogue can put a physical book back in
your hand. Built for the library that has no catalogue and no ISBNs to scan:
pre-barcode collections, where copy-cataloguing has nothing to copy from.

No install for the librarian, no command line. A phone, a browser, and a URL.

```bash
git clone https://github.com/quiquae/shelfmark && cd shelfmark
python -m venv .venv && .venv/bin/pip install -r requirements.txt
SHELFCAT_FAKE_VISION=1 .venv/bin/uvicorn web.app:app --port 8031
# open http://127.0.0.1:8031
```

`SHELFCAT_FAKE_VISION=1` runs the whole loop with three hardcoded spines per
crop, so you can see it work before wiring in a vision backend. Without it,
`spines.read_spine()` raises — a stub must never quietly become the product.

### What it does that other tools do not

Every AI cataloguing tool available today catalogues **one book at a time**
from an image. shelfmark catalogues **a shelf**, and keeps the order:
thirty-odd spines from one photograph, merged across overlapping frames,
each landing in MARC `952$o` as a call number you can walk to.

It is also built to be trusted rather than believed. A spine is
**transcribed, never identified** — the vision schema has no year field and
no ISBN field on purpose, because a model asked for them invents plausible,
wrong ones. Nothing is ever dropped: an unreadable spine still produces a
MARC record, with its shelf position, flagged for review. Every record
carries the frames it came from and how many times it was read.

> The package inside is called `shelfcat`, which is what this was called
> before it had a web interface. The name is load-bearing in imports and is
> deliberately not renamed.

## Status — full run complete, 1 Sep 2026

79 frames of the UATX library (IMG_2152–2230), all 24 MP, all passing the
sharpness gate:

**1,282 raw spine reads → 1,017 unique books**, 37 shelf rows across 13 shelves.
Every spine has a row, including the 103 that could not be read at all.

Transcription was independently validated: an agent's blind re-read of
IMG_2152 matched a separate manual transcription **14/14 spines, 100% on both
title and volume** — including two adjacent copies of Carlyle vol. 34 and a
vol. 33 shelved out of sequence.

## Finding rows without overlap — the side-gap signal

The capture assumed 2+ overlapping photographs per shelf row. In practice most
consecutive frames shared no books at all, and overlap alone cannot tell
"next photo, same row" from "next photo, new row" — that is absence of
evidence, not evidence of a boundary.

The physical signal that resolves it: **a bookcase upright or shelf end leaves
empty board beside the books.** `bands.book_extent()` already measures where
books start and stop horizontally, so the gap is free.

Measured over the 79 frames: of 29 frames whose books stop ≥8% of frame width
short of the right edge, **28 are followed by a genuine row boundary**, and
exactly one contradicts an overlap-based join. Applying it merged 14 evidence-free
splits and took 50 raw layers down to 36 rows — **exactly divisible by the 3
rows per shelf the photographer described**, which nothing in the pipeline
was told to aim for.

Merges made without overlap are recorded individually in the workbook: book
order across those seams follows photo order and is not verified by shared books.

## Why it is built this way

Cataloguing from images has one dominant failure mode: a plausible guess written
into a database field, where it becomes indistinguishable from a fact.

- **A spine is transcribed, never identified.** No year field, no ISBN field —
  neither can be read from a spine, and a model asked for them invents them.
- **Overlap is corroboration, not waste.** 2+ photographs per shelf layer means
  most books are read twice, independently. Agreement raises confidence;
  disagreement is a precise, self-generated uncertainty flag.
- **Nothing is dropped.** An unreadable spine still gets a row, with its shelf
  position and a link to the frame it came from.
- **Tiers never rise silently.** `db.set_tier` raises unless a reviewer is named.

## Pipeline

| Stage | Module | What it does |
|---|---|---|
| 1 | `ingest.py` | HEIC→JPEG, EXIF orientation, sharpness gate, capture order |
| 2 | `bands.py` | Finds the shelf layer each frame is *of*; trims to the books |
| 3 | `crops.py` | Renders transcription-sized crops at native resolution |
| — | *(vision)* | One JSON per frame in `transcripts/` — a swappable file interface |
| 4 | `stitch.py` | Segments layers, merges overlaps, corroborates reads |
| 5 | `stitch.py` | Volume-sequence anomalies, duplicate copies, suspect joins |
| 6 | `excel.py` | Workbook carrying the uncertainty in the rows |

`pipeline.py` runs the whole thing.

## Measured behaviour

**Overlap stitching under OCR noise** (`tests/test_stitch.py`, 5 seeds × 8 layers):

| char noise | layers found (exp. 8) | book count error |
|---|---|---|
| 0–5% | 8.0 | exact |
| 8% | 8.4 | +0.9% |
| 10% | 8.6 | +1.4% |
| 20% | 14.6 | +15.0% |

Degradation is **one-directional**: noise produces duplicates from missed joins,
never lost books from false joins. Over-splitting is recoverable; deletion is not.
`find_suspect_joins()` re-checks every boundary at a looser threshold to catch it.

**Barcode decoders** (`tests/scale_threshold.py`): ZBar holds to ~2.1 px/module,
ZXing needs ~3.2 — so ≥300 px of barcode width. Variance-of-Laplacian *rose* as
barcodes shrank past the decode limit, so sharpness is not a valid gate there;
`barcodes.capture_advice()` gates on measured width instead.

## Three bugs real data found that synthetic tests did not

1. **Multi-volume sets share one title.** Every spine of the Carlyle Letters run
   reads "The Collected Letters of Thomas and Jane Welsh Carlyle", so a
   title-only aligner matched everything to everything. Matching is now
   volume-aware: same title + different volume is a hard veto.
2. **Title-only duplicate detection over-reported 22×.** One shelf of a 40-volume
   set produced 22 "duplicates" where there is exactly one — a genuine second
   copy of vol. 34. Duplicates now require title *and* volume to agree.
3. **`ws.append()` silently skipped a row.** Setting `freeze_panes` materialises
   row 2, so the first append landed on row 3, leaving a blank row that broke
   every `COUNTA`. Rows are now written by explicit index.

## Run

```bash
pip install -r requirements.txt          # + apt install libzbar0 / brew install zbar
python -c "from shelfcat.pipeline import prepare; prepare('photos/', 'work/')"
# transcribe work/crops/*.jpg -> work/transcripts/<frame>.json
python -c "from shelfcat.pipeline import catalogue; catalogue('work/transcripts','work/manifest.json','catalogue.xlsx')"
```

Tests: `for t in tests/test_*.py; do python $t; done`

## Workbook

| Sheet | Purpose |
|---|---|
| Summary | Live counts over the Catalogue sheet — correcting a row updates them |
| Catalogue | One row per spine seen. Nothing dropped. |
| Review queue | Only the rows a person must resolve, most urgent first |
| Anomalies | Volume-sequence problems, duplicate copies, uncertain joins |
| Frames | One row per photograph — the audit trail back to the pixels |

Confidence: `high` = clearly legible **and** read in 2+ frames · `medium` = clear
but seen once, or partial but corroborated · `low` = partial and seen once, or
frames disagreed · `none` = unreadable.

## Known gaps

- `authorities.py` HTTP layer still unverified — the build container blocks
  openlibrary.org and googleapis.com. Run `tests/enrich_probe.py` on a networked
  machine before trusting it.
- Band detection assumes one fully-visible layer per frame; a frame showing two
  complete layers will catalogue only one.
- No barcode path wired into the shelf pipeline yet — `barcodes.py` is tested
  standalone.

---

## Web application

A mobile-first web app over the same package: photograph a shelf on a phone,
upload it, download `catalogue.mrc`. No install for the librarian, no command
line, one codebase served from one backend.

```bash
pip install -r requirements.txt
SHELFCAT_FAKE_VISION=1 uvicorn web.app:app --reload --port 8031
# open http://127.0.0.1:8031
```

`SHELFCAT_FAKE_VISION=1` makes `spines.read_spine()` return three hardcoded
spines per crop, one of them deliberately illegible, so the whole loop is
exercisable before a real vision backend exists. Without it `read_spine`
raises — a stub must never quietly become the product.

### How a request becomes a catalogue

Image work does not run inside the request. `POST /upload` writes the frames,
creates a row in `jobs`, and redirects; a single daemon worker thread claims
the row and runs the existing pipeline, writing its stage and progress back to
that row for `GET /api/jobs/{id}` to read.

| Stage | Calls | Notes |
|---|---|---|
| ingest, crops | `pipeline.prepare` | HEIC/EXIF, sharpness gate, band detection, overlapping crops |
| vision | `spines.transcribe_frame` | one JSON per frame in `transcripts/`, as before |
| stitch | `pipeline.catalogue` | overlap merge, row regrouping, shelf assignment, workbook |
| resolve | `authorities.resolve` | Open Library then Google Books, scored by `score_match` |
| export | `export.to_csv`, `export.to_marc` | CSV and MARC21 |

Nothing is reimplemented in the web layer. `web/jobs.py` orchestrates and
persists; `web/app.py` accepts files and serves what the worker wrote.

### Why MARC field 952

A small library evaluates this by importing `catalogue.mrc` into Koha and
seeing whether the books can then be found. Koha keeps item-level data in
**952**, its local-use field, because only a few 852 subfields are free in
MARC21 and Koha needed columns the standard does not have. Shelf order written
only to 852 imports as bibliographic holdings text and never becomes findable
item data. Every record therefore carries both: `952$o` with the call number
for Koha and Evergreen, and 852 for anything else.

### Nothing is dropped, end to end

An unreadable spine gets a CSV row and a MARC record with `245` reading
`[Spine not legible]`, its shelf position in `952$o`, a review note in
`952$z`, and its source frames in `500`. A record with no `245` is rejected
outright by an ILS, which would make the volume vanish — so the placeholder is
load-bearing, not cosmetic. Files rejected at upload are named in the job's
warnings and moved to `work/rejected/`, never deleted.

### Measured on the reference run

Every number below comes from the 1,017-book run in `work/transcripts`, not
from a synthetic fixture.

| | |
|---|---|
| spines resolving to at least one candidate | **99%** (138/140 sampled) |
| whole library resolved | **under 6 minutes** (0.52s/spine, 679 distinct queries) |
| auto-acceptable, as shipped in v1.0 | 32% |
| auto-acceptable now | **64%** |
| review queue on 1,017 volumes | 692 → **366** |
| review time at 3s/item | 35 min → **18 min** |
| unreadable spines (a floor, not a defect) | 170 (17%) |
| corroborated by a second frame | 233 (23%) |

Two changes produced that, both measured rather than assumed:

**Editions collapse before tiering.** `tier_for` downgrades a result when a
second candidate scores close, because ambiguity is the real risk. But almost
every close second is another *edition of the same work*: Open Library returns
five rows for "To the Lighthouse" that are three works, and the old tiering
called that ambiguous and sent a score of 1.00 to review.
`collapse_editions()` groups by (normalised title, author surname) and tiers on
the best score per distinct work. Auto-accept 32% → 57%.

**The author-contradiction cap stops misfiring on editors.** A scholarly
edition credits its *editor* on the spine and its *author* in the authority
record, so the two contradict by construction: "De Quincey's Works / Masson"
against "De Quincey's works / Thomas De Quincey". Eight labelled pairs sat at
exactly 0.44 — `min(title, 0.55) × 0.8` — for that reason alone. The cap now
softens only when the record's author is named in the title *and* the title is
of a collected edition; "John Clare" by Jonathan Bate against "John Clare" by
John Clare stays capped, because a biography whose title is its subject is a
different book. A subtitle present on one side only is also tolerated.
Auto-accept 57% → 64%.

### The threshold is calibrated, not inherited

```bash
python tests/calibrate_threshold.py     # reads tests/labels.csv
```

`REVIEW_BELOW = 0.82` decides which volumes a machine may assert unreviewed,
so it is the number every accuracy claim rests on. Against 98 labelled real
spine/candidate pairs:

| threshold | auto-accepted | wrong | precision | recall |
|---|---|---|---|---|
| 0.57 | 80 | 1 | 0.988 | 1.000 |
| 0.74 | 78 | 0 | 1.000 | 0.987 |
| **0.82** | **77** | **0** | **1.000** | **0.975** |
| 0.88 | 53 | 0 | 1.000 | 0.671 |

0.82 is kept. It auto-accepts nothing wrong, and the highest-scoring *wrong*
match in the set is 0.72 — so 0.82 carries a 0.10 margin, where the
technically optimal 0.74 sits one sample away from admitting errors. For a
catalogue, precision is the expensive side: a wrong record asserted without
review is indistinguishable from a fact, while a correct record sent to review
costs a few seconds.

`tests/labels.csv` ships with the repo, so anyone can rerun this. It was
labelled by bibliographic judgement — "does this
candidate denote the same *work*" — and **not** by physical verification.
Relabel it at the shelf and rerun; that is a stronger set and the numbers will
move.

### Work that only a person at the shelf can do

`/job/{id}/worklist` is the list you print or carry on a phone: unreadable
spines, and sets whose volume numbers could not be read. Ordered by shelf then
position so it matches the walk, each row linking to its review screen so the
answer can be typed in while standing in front of the book. Also available as
CSV.

170 of 1,017 spines (17%) are unreadable. No amount of model quality changes
that — a dark spine with no legible text is not recoverable from a photograph
at any resolution — so it is surfaced as a task rather than hidden as a defect.

### Classification

Dewey comes from the authority record, never from a model. Measured on 40 real
spines: 55% carry a `ddc`, 84% an `lcc`, 87% subject terms. Written to MARC
**082** (with `$2 23`), **050** and **650**. Shelf order stays in `952$o` —
classification does not overwrite the thing that finds the physical book.

### Tests

```bash
python tests/test_web.py        # library units: export, scoring, crop rejoin
python tests/test_review.py     # the whole HTTP path via TestClient
```

Both run with no network and no server, and both exit non-zero on failure.

The older `tests/test_*.py` scripts print `FAIL` but always exit 0, so
`for t in tests/test_*.py; do python $t; done` reports green over a failing
suite. `test_stitch.py` currently fails 3 of 33 assertions; see CLAUDE.md.


---

## Licence

Copyright (C) 2026 Creagh Factor.

shelfmark is free software under the **GNU Affero General Public License,
version 3 or later**. You may use, study, modify and redistribute it. If you
run a modified version as a network service, the AGPL requires you to offer
your users the source of that version. The full text is in
[LICENSE](LICENSE).

AGPL was chosen deliberately for a library tool. Koha itself is GPL, so
copyleft is familiar and uncontroversial in this sector, and it keeps the
work open while preventing a closed re-hosting of it. If you want to use
shelfmark under different terms, ask.

## Status

**1.0.0** — the loop works end to end and is measured on a real 1,017-book
collection. Not yet: multi-collection sessions, a documented deployment path,
and a real vision backend (the seam is defined; supply `read_spine`).

Contributions welcome, particularly a `read_spine` implementation that emits
the `bbox` field `spines.SPINE_SCHEMA` already asks for — that is what turns
the approximate spine crop in the review screen into an exact one.
