# shelfmark / shelfcat — working rules

Photo-to-catalogue pipeline for a physical library. A validated run over the
UATX library exists: 79 frames, 1,282 raw spine reads, 1,017 unique books,
37 shelf rows. A blind re-read of IMG_2152 matched a manual transcription
14/14 on title and volume.

The package is `shelfcat/`. **Do not rename it** — a rename is a rewrite.

## Must not be rewritten

Every module below encodes behaviour that was calibrated against real data.
Extend if genuinely needed; never replace, never reimplement in the web layer.

| File | What is load-bearing |
|---|---|
| `shelfcat/stitch.py` | `OVERLAP_SIM = 85.0` separates "Canterbury Tales"/"The Canterbury Tales" (89) from "Troilus and Criseyde"/"Troilus and Cressida" (80). `Read.match_key()` is volume-aware because a 40-volume set puts one title on every spine. |
| `shelfcat/bands.py` | `book_extent()` supplies the side-gap signal that merged 14 evidence-free row splits (50 layers → 36 rows). |
| `shelfcat/location.py` | `assign`/`validate`/`regroup_rows` — shelf/row/position assignment and its warnings. |
| `shelfcat/ingest.py` | HEIC EXIF orientation, sharpness gate (`BLUR_FLOOR = 45.0`), capture-order cross-check. |
| `shelfcat/crops.py` | Crops at native resolution *then* scales once. Rendering a full frame throws away the detail spine text needs. |
| `shelfcat/db.py` | Four-table provenance chain (images → evidence → claims → records). `set_tier()` refuses silent promotion. |
| `shelfcat/excel.py` | Workbook that carries uncertainty in the rows. Rows are written by explicit index — `ws.append()` with `freeze_panes` silently skips row 2. |
| `shelfcat/spines.py` | `SPINE_SCHEMA` and `PROMPT`. The schema has no year and no ISBN **on purpose**: a model asked for them invents them. |
| `shelfcat/authorities.py` | `score_match` / `tier_for` are pure and tested. Text evidence can never reach `green` — barcode only. |
| `tests/test_*.py` | Must pass unchanged at every milestone. |

## Known state of the code — do not assume otherwise

- `authorities.py` **has no HTTP functions.** Its docstring describes
  `isbn_lookup()` and `text_search()`; neither is implemented. `requests` and
  `isbnlib` are declared but unused. Resolution against Open Library /
  Google Books does not exist yet.
- **There is no MARC export.** `excel.py` is the only exporter.
- `db.py` is **not wired into `pipeline.py`**. The pipeline goes
  transcripts → stitch → location → excel and never touches SQLite.
- The 79 real transcripts use `{i, title, author, volume, detail, publisher,
  legibility, at_edge, note}` — they do **not** carry `bbox`, even though
  `SPINE_SCHEMA` requires it. There are no per-spine crop coordinates.
- `streamlit` in `requirements.txt` refers to a `cli.py` that does not exist.

## Rules

1. Never silently drop a volume. An unreadable spine still gets a row with its
   shelf position and a link back to the frame.
2. Tiers rise only through named human review.
3. A spine is **transcribed, never identified**. No year, no ISBN from pixels.
4. No new top-level directories beyond `web/` and `static/`. Keep the file
   count low — if you are about to create a fifth file to do one job, simplify.
5. No `v2.py`, `_new`, `_final`, `_old`. Edit in place.
6. Commit at the end of a milestone, not during. No branches.
7. Ask before adding a dependency. Name it, say what it replaces, and why
   hand-rolling is worse.
8. No institutional data in the repo. `work/` is gitignored and that is
   deliberate — the licence is unchosen and UATX ownership is unresolved.
9. If unsure whether an API behaves as you think, say so instead of writing
   code that assumes it. A blank with an explanation beats a confident guess.

## MARC target

Koha stores item-level data in **952**, its local-use field, not 852. Shelf
order written only to 852 imports as bibliographic holdings text and does not
become findable item data. Write `952$o` (full call number) plus `952$a`/`$b`
for the library, and keep 852 for portability to non-Koha systems.
