import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shelfcat.authorities import score_match, tier_for, normalise, surname

print("--- normalisation ---")
for s in ["The Canterbury Tales", "CANTERBURY TALES, THE", "Chaucer's Dream-Poetry", "Le Morte d'Arthur"]:
    print(f"  {s!r:<34} -> {normalise(s)!r}")
print("\n--- surname extraction ---")
for a in ["Geoffrey Chaucer", "Chaucer, Geoffrey", "J. R. R. Tolkien", "Bede"]:
    print(f"  {a!r:<24} -> {surname(a)!r}")

print("\n--- match scoring ---")
cases = [
    ("Canterbury Tales", "Chaucer",  "The Canterbury Tales", "Geoffrey Chaucer", "exact-ish, author confirms"),
    ("Canterbury Tales", None,       "The Canterbury Tales", "Geoffrey Chaucer", "no author read"),
    ("Canterbury Tales", "Chaucer",  "Canterbury Tales",     "Peter Ackroyd",    "AUTHOR CONTRADICTED -> must cap"),
    ("Troilus and Criseyde","Chaucer","Troilus and Cressida","William Shakespeare","similar title, wrong author"),
    ("Piers Plowman",    "Langland", "The Vision of Piers Plowman", "William Langland","partial title"),
    ("Beowulf",          "Heaney",   "Beowulf",              "Seamus Heaney",   "short title exact"),
]
for rt, ra, ct, ca, why in cases:
    print(f"  {score_match(rt,ra,ct,ca):>6}  {why}")
    print(f"          read={rt!r}/{ra!r}  cand={ct!r}/{ca!r}")

print("\n--- tiering from candidate distributions ---")
for scores, why in [([0.97,0.55],"strong + clear gap"), ([0.97,0.94],"strong but AMBIGUOUS"),
                    ([0.80,0.60],"moderate + gap"), ([0.80,0.78],"moderate + ambiguous"),
                    ([0.40],"weak"), ([],"nothing found")]:
    print(f"  {str(scores):<14} -> {tier_for(scores):<6}  ({why})")
print("\nNote: text evidence never yields 'green' by design; only a decoded barcode does.")
