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

- The 79 real transcripts use `{i, title, author, volume, detail, publisher,
  legibility, at_edge, note}` — they do **not** carry `bbox`, even though
  `SPINE_SCHEMA` requires it. **There are no per-spine crop coordinates
  anywhere in the system.** A review screen can therefore show the band crop
  and an ordinal ("spine 3 of 14"), not an individual spine image. Emitting
  `bbox` from a real `read_spine` is what unlocks per-spine crops, and
  `SPINE_SCHEMA` already demands it.
- `read_spine()` has **no real implementation** and must not get one by
  accident. Without `SHELFCAT_FAKE_VISION=1` it raises. `evidence.detector`
  records `vlm:fake` or `vlm:real` so a fake run can never later be mistaken
  for a real one.
- `tests/test_stitch.py` fails **3 of 33** assertions, in scenario 11 only,
  and did so before any of this work. It feeds `"De Temporum Ratione"` against
  `"De Temporum Rati0ne"` and expects the strict pass to refuse the join; the
  pair scores **94.7** against `OVERLAP_SIM = 85`, so it merges. A
  one-character error in a nineteen-character string cannot score below 85.
  The implementation follows its documented calibration and the test's
  expectation is stale. **Do not "fix" the code to satisfy it.**
- Every `tests/test_*.py` script except `test_web.py` **exits 0 even when
  assertions fail.** Chaining them in CI reports green over a red suite.
- `streamlit` in `requirements.txt` referred to a `cli.py` that never existed.
  Removed; the UI is `web/`.

### Measured on the reference run — do not re-derive these by guessing

| | |
|---|---|
| auto-acceptable, v1.0 → now | 32% → **64%** |
| review queue on 1,017 volumes | 692 → **366** |
| spines with at least one candidate | 99% |
| whole library resolved | under 6 min (0.52s/spine) |
| unreadable spines | 170 (17%) — a floor, surfaced as shelf work |
| corroborated by a 2nd frame | 233 (23%) |
| `REVIEW_BELOW = 0.82` | precision 1.000, recall 0.975 on 98 labelled pairs |

Rerun the calibration before touching the threshold:
`python tests/calibrate_threshold.py`. 0.82 is kept deliberately — the
highest-scoring *wrong* match in the labelled set is 0.72, so 0.82 holds a
0.10 margin. `work/labels.csv` is bibliographic judgement, not physical
verification; relabel at the shelf for a stronger set.

**52 rows, not 37.** `pipeline.catalogue` without a `manifest.json` skips
`regroup_rows`, so the side-gap row merge never runs and the published
37-row figure is not reproducible from the shipped artefacts. The 1,017 book
count does reproduce exactly.

### Added after v1.0 (was missing, now real)

- `authorities.py` gained the HTTP layer its docstring had always described:
  `isbn_lookup`, `text_search`, `resolve`, and the `--selftest` it pointed at.
  **The author is never a filter** — Open Library's `author=` and Google
  Books' `inauthor:` are AND constraints, so a spine reading
  "Ryals & Fielding" zeroed the whole result set. Measured: 0 candidates with
  the author filter, 5 without. The title filters; the author scores.
- Google Books' unauthenticated endpoint returns **429** from a shared IP, so
  a circuit breaker drops an authority after 3 consecutive rate-limits rather
  than spending ~1.8s of backoff per spine on a host that will not answer.
  Affected volumes are flagged; they do not look merely "not found".
- `shelfcat/export.py` writes CSV and MARC21.
- `spines.transcribe_frame()` rejoins a frame's overlapping crops using
  `stitch.best_overlap` — the same calibrated alignment, not a second rule.
  Without it every spine near a crop boundary became a phantom second copy.
- `pipeline.catalogue()` additionally returns `layers` so a caller can build
  records without re-running the merge. The workbook is written as before.
- `db.py` is now wired in, by `web/jobs.py`: images → evidence → claims →
  records, with `job_items` holding job ownership so the provenance schema
  itself is untouched.
- `collapse_editions()` groups candidates by (normalised title, author
  surname) and `tier_for_candidates()` tiers on distinct works. `tier_for`
  itself is unchanged, so `test_scoring.py` still holds.
- `score_match` no longer hard-caps the editor/author artefact. A scholarly
  edition credits its editor on the spine and its author in the record, so
  they contradict by construction. The cap softens ONLY when the record's
  author is named in the title AND a collected-edition word is present —
  both conditions are load-bearing, because "John Clare" by Jonathan Bate vs
  "John Clare" by John Clare passes on the surname alone and they are
  different books. `_title_sim` also tolerates a subtitle on one side only.
- `duplicate_report` ranks claims `confirmed`/`likely`/`possible` instead of
  listing 248 flat, and gates the prefix rule on `at_edge`.
  `unmarked_set_report` is what the weak claims become.
- The review screen (`web/templates/review.html`) and
  `jobs.apply_review` / `review_queue` / `record_detail` / `spine_window`.
  Exports regenerate on download so corrections reach the file.
- `/job/{id}/worklist` — unreadable spines and volume-number checks, in
  walking order, printable, CSV.

### Traps that already cost time

- The base `button,.button{color:#fff}` rule blankets every button. Any new
  button that is not a filled accent button must take `color:var(--ink)` or it
  renders white-on-white. This silently hid the whole candidate list.
- `Read.index` is documented 0-based but `load_transcripts` fills it from the
  transcript's `i`, which is 1-based in all 79 real transcripts. `stitch` only
  uses it for ordering, so the mismatch surfaces only where it indexes a list.
- macOS Chrome clamps `--window-size` to ~500px minimum, so a 390px
  screenshot is laid out at 500 and then cropped — which looks exactly like a
  layout bug. Use CDP `Emulation.setDeviceMetricsOverride`.
- `excel.build` does not create its output directory.
- A parking directory for rejected uploads must live OUTSIDE the upload dir,
  because `pipeline.prepare` globs `*` and will try to decode it.

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
