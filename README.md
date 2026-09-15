# shelfcat

Photo-to-catalogue pipeline for a large physical library. Shelf photographs in,
searchable Excel catalogue out, with an explicit confidence tier and a full
provenance trail on every row.

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
