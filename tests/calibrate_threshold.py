"""Calibrate export.REVIEW_BELOW against labelled matches.

The threshold decides which volumes a machine may assert without a human
looking, so it is the number every accuracy claim rests on. Guessing it is not
acceptable and neither is inheriting it.

    python tests/calibrate_threshold.py [labels.csv]

The CSV needs three columns: score, correct (1/0), and note. work/labels.csv
is a starter set of 98 real spine/candidate pairs from the UATX run, labelled
by bibliographic judgement -- "does this candidate denote the same WORK as the
spine" -- and NOT by physical verification. Replace it with labels made at the
shelf and rerun; that is a stronger set and the numbers will move.

Prints precision, recall and F1 at each threshold so the trade-off is visible
rather than collapsed into one recommended number. For a catalogue, precision
is the expensive side: a wrong record asserted without review is
indistinguishable from a fact, while a correct record sent to review costs
only a few seconds.
"""
import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from shelfcat.export import REVIEW_BELOW

DEFAULT = pathlib.Path(__file__).resolve().parent.parent / "work" / "labels.csv"


def load(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not (r.get("score") or "").strip():
                continue                        # no candidate: nothing to tier
            rows.append((float(r["score"]), int(r["correct"]), r.get("note", "")))
    return rows


def curve(rows):
    thresholds = sorted({round(s, 2) for s, _, _ in rows} | {REVIEW_BELOW})
    out = []
    n_correct = sum(c for _, c, _ in rows)
    for t in thresholds:
        tp = sum(1 for s, c, _ in rows if s >= t and c)
        fp = sum(1 for s, c, _ in rows if s >= t and not c)
        fn = sum(1 for s, c, _ in rows if s < t and c)
        prec = tp / (tp + fp) if tp + fp else 1.0
        rec = tp / n_correct if n_correct else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        out.append({"t": t, "tp": tp, "fp": fp, "fn": fn,
                    "precision": prec, "recall": rec, "f1": f1})
    return out


def main(path):
    rows = load(path)
    n = len(rows)
    print(f"{n} labelled pairs · {sum(c for _, c, _ in rows)} correct · "
          f"{n - sum(c for _, c, _ in rows)} wrong\n")
    print(f"{'thresh':>7} {'auto':>5} {'wrong':>6} {'missed':>7} "
          f"{'precision':>10} {'recall':>8} {'F1':>7}")
    c = curve(rows)
    for r in c:
        mark = "  <- shipped" if abs(r["t"] - REVIEW_BELOW) < 1e-9 else ""
        print(f"{r['t']:7.2f} {r['tp'] + r['fp']:5d} {r['fp']:6d} {r['fn']:7d} "
              f"{r['precision']:10.3f} {r['recall']:8.3f} {r['f1']:7.3f}{mark}")

    clean = [r for r in c if r["fp"] == 0]
    best_clean = max(clean, key=lambda r: r["recall"]) if clean else None
    best_f1 = max(c, key=lambda r: r["f1"])
    print()
    if best_clean:
        print(f"lowest threshold with NO wrong record auto-accepted: "
              f"{best_clean['t']:.2f}  (auto-accepts {best_clean['tp']}, "
              f"recall {best_clean['recall']:.2f})")
    print(f"best F1: {best_f1['t']:.2f}  (precision {best_f1['precision']:.3f}, "
          f"recall {best_f1['recall']:.3f}, lets through {best_f1['fp']} wrong)")
    shipped = next(r for r in c if abs(r["t"] - REVIEW_BELOW) < 1e-9)
    print(f"shipped {REVIEW_BELOW}: precision {shipped['precision']:.3f}, "
          f"recall {shipped['recall']:.3f}, "
          f"{shipped['fp']} wrong auto-accepted, {shipped['fn']} correct sent to review")

    # Where is the recall going? If correct matches cluster at one score, that
    # is a scoring bug, not a threshold problem, and moving the threshold to
    # catch them would drag the wrong ones with it.
    print("\ncorrect matches sent to review, by score:")
    buckets = {}
    for s, ok, note in rows:
        if ok and s < REVIEW_BELOW:
            buckets.setdefault(round(s, 2), []).append(note)
    for s in sorted(buckets, reverse=True):
        notes = [x for x in buckets[s] if x][:2]
        print(f"  {s:.2f}  {len(buckets[s]):3d}  {'; '.join(notes)[:64]}")
    capped = sum(len(v) for k, v in buckets.items() if 0.42 <= k <= 0.45)
    if capped:
        print(f"\n{capped} correct matches sit at ~0.44, which is exactly "
              f"min(title,0.55)*0.8 -- the author-contradiction cap in "
              f"score_match. On a scholarly edition the spine credits the "
              f"EDITOR and the authority credits the author, so the cap "
              f"misfires. That is a scoring fix, not a threshold fix: "
              f"lowering the threshold to catch these would admit the "
              f"genuinely wrong matches sitting at the same score.")
    return 0


if __name__ == "__main__":
    p = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not p.exists():
        print(f"no labels at {p}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(p))
