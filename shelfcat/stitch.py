"""Layer segmentation and overlap merge.

The capture protocol takes 2+ overlapping photographs per shelf layer, which
creates one problem and hands us one gift.

  Problem: the same book appears in several frames. Naive concatenation
           inflates the catalogue with duplicates.
  Gift:    a book seen in two frames has been read twice, independently.
           Agreement between those reads is free corroboration; disagreement
           is a precise, self-generated uncertainty flag.

Both are handled by the same operation: align the tail of one frame's book
list against the head of the next, using fuzzy title similarity.

Layer boundaries fall out of the same alignment. Consecutive frames from one
layer overlap; frames either side of a layer break do not. So the sequence
segments itself and the operator never has to declare where a layer ends.
"""
import re
import unicodedata
from dataclasses import dataclass, field
from rapidfuzz import fuzz

# An overlap must clear this mean similarity to count as a real overlap.
# Calibrated against known match/non-match pairs -- see tests/test_stitch.py.
# 85 separates "Canterbury Tales"/"The Canterbury Tales" (89, match) from
# "Morte Darthur"/"Mort Artu" (82) and "Troilus and Criseyde"/"Troilus and
# Cressida" (80), both of which must NOT merge.
OVERLAP_SIM = 85.0
# A single shared book is a weak join; two or more is trustworthy.
STRONG_OVERLAP_MIN = 2
# Below this, two reads of the same slot are treated as disagreeing.
AGREE_SIM = 85.0

LEGIBILITY_RANK = {"clear": 3, "partial": 2, "illegible": 1, None: 0}


@dataclass
class Read:
    """One observation of one spine in one photograph."""
    image: str
    index: int                 # position within that photograph, 0-based
    title: str | None
    author: str | None
    legibility: str = "clear"
    script: str = "latin"
    item_type: str = "book"
    volume: str | None = None
    at_edge: bool = False      # first or last spine in frame: may be cut off

    def match_key(self) -> str:
        """What identifies this spine for alignment purposes.

        Title alone is not enough. A multi-volume set -- the Carlyle Letters
        run in frames IMG_2152-53 is 40+ volumes -- puts the SAME title on
        every spine, so a title-only aligner matches every book to every
        other and the overlap search degenerates. The volume marking is the
        only field that separates them, so it joins the key."""
        t = (self.title or "").strip()
        v = (self.volume or "").strip()
        return f"{t} || {v}" if v else t


@dataclass
class Book:
    """One physical volume, after merging every read of it."""
    title: str | None = None
    author: str | None = None
    legibility: str = "clear"
    script: str = "latin"
    item_type: str = "book"
    volume: str | None = None
    reads: list = field(default_factory=list)
    variants: list = field(default_factory=list)   # conflicting titles seen
    flags: list = field(default_factory=list)

    @property
    def n_reads(self) -> int:
        return len(self.reads)

    @property
    def sources(self) -> list:
        return [r.image for r in self.reads]


_ARTICLES = ("the ", "a ", "an ", "le ", "la ", "les ", "der ", "die ", "das ")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    for art in _ARTICLES:
        if s.startswith(art):
            return s[len(art):]
    return s


def read_sim(a: "Read", b: "Read") -> float:
    """Similarity of two spine reads, volume-aware.

    Within a multi-volume set the titles are identical and only the volume
    differs, so a volume mismatch must veto the match outright -- otherwise
    Volume 29 merges happily into Volume 33."""
    ta, tb = _sim(a.title, b.title), None
    if ta < OVERLAP_SIM:
        return ta
    va, vb = (a.volume or "").strip(), (b.volume or "").strip()
    if va and vb:
        vs = _sim(va, vb)
        if vs < 90:
            return min(ta, 40.0)          # same title, different volume: veto
        return ta
    if va or vb:
        return ta * 0.9                   # one side unread: usable, not certain
    return ta


def _sim(a: str | None, b: str | None) -> float:
    """Similarity of two spine reads. Two illegibles are not a match --
    absence of text is not evidence of sameness.

    A plain fuzzy ratio cannot do this job alone. The two error modes look
    nothing alike:

      truncation  a spine cut off by the frame edge reads as a PREFIX of the
                  full title ("Sir Gawain" / "Sir Gawain and the Green
                  Knight"): ratio 49, but certainly the same book.
      confusion   two genuinely different works with similar names ("Morte
                  Darthur" / "Mort Artu": 82; "Troilus and Criseyde" /
                  "Troilus and Cressida": 80) must never merge.

    Treating prefixes as a separate case lets the ratio threshold sit high
    enough to reject the confusions without losing the truncations."""
    if not a or not b:
        return 0.0
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 6 and long.startswith(short):
        return 97.0
    return fuzz.ratio(na, nb)


def best_overlap(left: list[Read], right: list[Read], max_k: int | None = None):
    """Find k such that the last k of `left` are the same books as the first k
    of `right`. Returns (k, mean_similarity). k == 0 means no overlap, which
    is read as a layer boundary.

    Longer overlaps are preferred at comparable quality, because a long
    agreeing run is far less likely to be coincidence than a short one."""
    hi = min(len(left), len(right))
    if max_k:
        hi = min(hi, max_k)
    best = (0, 0.0)
    for k in range(hi, 0, -1):
        pairs = list(zip(left[-k:], right[:k]))
        scored = [read_sim(a, b) for a, b in pairs]
        # Illegible slots neither help nor damn an alignment: drop them, but
        # require that something real was actually compared.
        usable = [s for s, (a, b) in zip(scored, pairs) if a.title and b.title]
        if not usable:
            continue
        mean = sum(usable) / len(usable)
        if mean >= OVERLAP_SIM and mean > best[1] + 1e-9:
            best = (k, mean)
        elif mean >= OVERLAP_SIM and k > best[0] and mean >= best[1] - 5:
            best = (k, mean)          # prefer the longer run on a near tie
    return best


def _merge_reads(reads: list[Read]) -> Book:
    """Collapse several reads of one spine into a single record.

    Preference order for which read supplies the text:
      1. better legibility  2. not cut off at a frame edge  3. longer text
    A book cut off at the edge of one frame is usually whole in the next; that
    is precisely what the overlap is for."""
    ranked = sorted(
        reads,
        key=lambda r: (LEGIBILITY_RANK.get(r.legibility, 0), not r.at_edge,
                       len(r.title or "")),
        reverse=True)
    primary = ranked[0]
    book = Book(title=primary.title, author=primary.author,
                legibility=primary.legibility, script=primary.script,
                item_type=primary.item_type, volume=primary.volume,
                reads=list(reads))

    titled = [r for r in reads if r.title]
    if len(titled) > 1:
        disagreeing = [r.title for r in titled[1:]
                       if read_sim(titled[0], r) < AGREE_SIM]
        if disagreeing:
            book.variants = sorted({titled[0].title, *disagreeing})
            book.flags.append("read_disagreement")
        else:
            book.flags.append("corroborated")

    # An author read in any frame beats no author in the chosen one.
    if not book.author:
        for r in ranked:
            if r.author:
                book.author = r.author
                book.flags.append("author_from_other_frame")
                break
    return book


def segment_and_merge(frames: list[tuple[str, list[Read]]]):
    """frames: ordered (image_name, reads) as captured, left to right.

    Returns (layers, joins) where a layer is a merged list of Books and joins
    records how each consecutive pair was resolved, so every boundary decision
    stays auditable."""
    layers, joins = [], []
    cur_frames: list[list[Read]] = []
    cur_names: list[str] = []

    def flush():
        if cur_frames:
            layers.append(_merge_layer(cur_frames))

    for i, (name, reads) in enumerate(frames):
        if not cur_frames:
            cur_frames.append(reads); cur_names.append(name); continue
        k, sim = best_overlap(cur_frames[-1], reads)
        joins.append({"left": cur_names[-1], "right": name, "overlap": k,
                      "similarity": round(sim, 1),
                      "verdict": ("same_layer" if k >= STRONG_OVERLAP_MIN else
                                  "same_layer_weak_join" if k == 1 else
                                  "layer_boundary")})
        if k == 0:
            flush(); cur_frames, cur_names = [reads], [name]
        else:
            cur_frames.append(reads); cur_names.append(name)
    flush()
    return layers, joins


def _merge_layer(frames: list[list[Read]]) -> list[Book]:
    """Chain-merge every frame in one layer into a single ordered book list."""
    merged: list[list[Read]] = [[r] for r in frames[0]]
    for nxt in frames[1:]:
        tail = [slot[-1] for slot in merged]      # latest read of each slot
        k, _ = best_overlap(tail, nxt)
        if k:
            for j in range(k):
                merged[len(merged) - k + j].append(nxt[j])
            merged.extend([[r] for r in nxt[k:]])
        else:
            merged.extend([[r] for r in nxt])     # defensive: shouldn't happen
    books = [_merge_reads(slot) for slot in merged]
    for b in books:
        if b.n_reads == 1:
            b.flags.append("single_read")
    return books


# Threshold for "these two layers might really be one". Deliberately lower
# than OVERLAP_SIM: this pass only raises a question for a human, it never
# merges anything, so it can afford to be suspicious.
SUSPECT_SIM = 70.0


def find_suspect_joins(layers: list[list[Book]], max_k: int = 4):
    """Safety net for the failure mode measured in tests/test_stitch.py.

    Under noisy reads the aligner misses genuine overlaps and splits one layer
    into two, which shows up as duplicate books rather than lost ones. This
    pass re-examines every layer boundary at a looser threshold and reports
    the ones worth a human glance. It returns findings; it changes nothing."""
    out = []
    for i in range(len(layers) - 1):
        left, right = layers[i], layers[i + 1]
        best = (0, 0.0)
        for k in range(min(max_k, len(left), len(right)), 0, -1):
            pairs = list(zip(left[-k:], right[:k]))
            usable = [_sim(a.title, b.title) for a, b in pairs if a.title and b.title]
            if not usable:
                continue
            mean = sum(usable) / len(usable)
            if mean >= SUSPECT_SIM and mean > best[1]:
                best = (k, mean)
        if best[0]:
            out.append({
                "left_layer": i, "right_layer": i + 1,
                "shared": best[0], "similarity": round(best[1], 1),
                "left_tail": [b.title for b in left[-best[0]:]],
                "right_head": [b.title for b in right[:best[0]]],
                "action": "review: these layers may be one, or these books may be duplicated",
            })
    return out


def duplicate_report(layers: list[list[Book]], threshold: float = 92.0):
    """Flag genuine duplicate copies anywhere in the catalogue.

    Title alone over-reports catastrophically here: every volume of a 40-book
    set shares one title, so a title-only check returned 22 "duplicates" for
    a shelf that has exactly one. Two books are duplicates only if title AND
    volume agree -- which is how the second copy of Carlyle vol. 34 in
    IMG_2152 is found without drowning it in false positives.

    Reports rather than removes: a duplicate copy is a real holding."""
    flat = [(li, bi, b) for li, layer in enumerate(layers)
            for bi, b in enumerate(layer) if b.title]
    seen, out = set(), []
    for i in range(len(flat)):
        for j in range(i + 1, len(flat)):
            (li, bi, a), (lj, bj, c) = flat[i], flat[j]
            va, vc = (a.volume or "").strip(), (c.volume or "").strip()
            if va or vc:
                if _sim(va, vc) < 90:      # same set, different volume
                    continue

            # A prefix match is the signature of a spine cut off by a frame
            # edge ("Sir Gawain" / "Sir Gawain and the Green Knight"), which
            # is why _sim scores it 97. Away from an edge it is simply a
            # different book: "George Eliot" is a prefix of "George Eliot A
            # Life" and the two are not copies of each other.
            if _prefix_only(a.title, c.title) and not (_at_edge(a) or _at_edge(c)):
                continue
            s = _sim(a.title, c.title)
            if s >= threshold and (li, bi, lj, bj) not in seen:
                seen.add((li, bi, lj, bj))
                adjacent = li == lj and abs(bi - bj) == 1
                # Ranked, not filtered. Nothing is dropped -- but an
                # unranked list of 248 claims, most of them weak, sends a
                # cataloguer to check the wrong thing and is read as the tool
                # crying wolf. Confidence says which ones to open first.
                #
                # confirmed: the standard the one shelf-verified duplicate met
                #   -- a second copy of Carlyle vol. 34, with the volume
                #   marking READ on both spines.
                # likely: no volume marking, but adjacent. A real second copy
                #   is nearly always shelved beside the first.
                # possible: no volume marking and not adjacent. Just as likely
                #   two volumes of a set whose numbering could not be read,
                #   which is why these also appear in unmarked_set_report,
                #   where the action is "read the volume numbers".
                if va and vc:
                    conf = "confirmed"
                elif adjacent:
                    conf = "likely"
                else:
                    conf = "possible"
                out.append({"kind": "duplicate", "confidence": conf,
                            "a": f"L{li}#{bi}", "b": f"L{lj}#{bj}",
                            "title_a": a.title, "title_b": c.title,
                            "volume": va or None,
                            "similarity": round(s, 1),
                            "adjacent": adjacent})
    out.sort(key=lambda d: {"confirmed": 0, "likely": 1, "possible": 2}[d["confidence"]])
    return out


def _at_edge(book: "Book") -> bool:
    return any(getattr(r, "at_edge", False) for r in book.reads)


def _prefix_only(a: str | None, b: str | None) -> bool:
    """True when the two titles match only because one is a prefix of the
    other -- the case _sim deliberately scores 97."""
    na, nb = _norm(a or ""), _norm(b or "")
    if not na or not nb or na == nb:
        return False
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(short) >= 6 and long.startswith(short)


def _unmarked_sets(flat, min_members: int = 2):
    """Titles carried by several books where no copy has a readable volume
    marking. Yields (normalised_title, [members])."""
    by_title = {}
    for item in flat:
        by_title.setdefault(_norm(item[2].title), []).append(item)
    for t, members in by_title.items():
        if len(members) >= min_members and not any(
                (b.volume or "").strip() for _, _, b in members):
            yield t, members


def unmarked_set_report(layers: list[list[Book]], min_members: int = 2):
    """Probable multi-volume sets whose volume numbering could not be read.

    This is a more useful finding than the duplicate flag it replaces: the
    action is "go and read the volume numbers on these N spines", and until
    someone does, their order within the set is unverified. Reporting them as
    duplicate copies instead sent a cataloguer to check the wrong thing."""
    flat = [(li, bi, b) for li, layer in enumerate(layers)
            for bi, b in enumerate(layer) if b.title]
    out = []
    for t, members in _unmarked_sets(flat, min_members):
        rows = sorted((li, bi) for li, bi, _ in members)
        out.append({"kind": "unmarked_set",
                    "title": members[0][2].title,
                    "n_members": len(members),
                    "locations": [f"L{li}#{bi}" for li, bi in rows],
                    "contiguous": all(
                        rows[k][0] == rows[0][0] and rows[k][1] == rows[0][1] + k
                        for k in range(len(rows)))})
    out.sort(key=lambda d: d["n_members"], reverse=True)
    return out


_VOL_NUM = re.compile(r"(\d+)")


def sequence_anomalies(layer: list[Book]):
    """Volume-order problems within one shelf layer.

    A run of numbered volumes is one of the few places where the catalogue can
    check itself: if the shelf reads 32, 34, 34, 33 then either the books are
    shelved out of order or a volume was misread, and both are worth a human
    glance. Gaps are reported separately because a gap usually means the
    library simply does not hold that volume."""
    nums = []
    for i, b in enumerate(layer):
        m = _VOL_NUM.search(b.volume or "")
        if m:
            nums.append((i, int(m.group(1)), b))
    out = []
    for j in range(len(nums) - 1):
        (i0, n0, b0), (i1, n1, b1) = nums[j], nums[j + 1]
        if n1 < n0:
            out.append({"kind": "out_of_order", "at": i1,
                        "detail": f"vol {n1} follows vol {n0}",
                        "legibility": b1.legibility})
        elif n1 > n0 + 1:
            out.append({"kind": "gap", "at": i1,
                        "detail": f"vols {n0 + 1}-{n1 - 1} absent between "
                                  f"vol {n0} and vol {n1}"})
    return out
