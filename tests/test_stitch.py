import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shelfcat.stitch import Read, segment_and_merge, best_overlap, _merge_reads, find_suspect_joins, duplicate_report

def R(img, i, t, a=None, leg="clear", edge=False):
    return Read(image=img, index=i, title=t, author=a, legibility=leg, at_edge=edge)

fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(name)

print("=== 1. clean overlap, no duplicates ===")
f1 = [R("a",0,"Canterbury Tales","Chaucer"), R("a",1,"Piers Plowman","Langland"),
      R("a",2,"Sir Gawain","Anon"), R("a",3,"Troilus and Criseyde","Chaucer",edge=True)]
f2 = [R("b",0,"Sir Gawain","Anon",edge=True), R("b",1,"Troilus and Criseyde","Chaucer"),
      R("b",2,"The Owl and the Nightingale",None), R("b",3,"Ancrene Wisse",None)]
layers, joins = segment_and_merge([("a",f1),("b",f2)])
titles = [b.title for b in layers[0]]
check("one layer", len(layers)==1, f"got {len(layers)}")
check("6 books not 8", len(layers[0])==6, f"got {len(layers[0])}: {titles}")
check("order preserved", titles[0]=="Canterbury Tales" and titles[-1]=="Ancrene Wisse", str(titles))
check("overlap detected k=2", joins[0]["overlap"]==2, str(joins[0]))
check("overlapped books have 2 reads", [b.n_reads for b in layers[0]]==[1,1,2,2,1,1],
      str([b.n_reads for b in layers[0]]))
check("corroboration flagged", "corroborated" in layers[0][2].flags, str(layers[0][2].flags))

print("\n=== 2. three frames chained in one layer ===")
g1=[R("a",0,"Beowulf"),R("a",1,"The Wanderer"),R("a",2,"The Seafarer")]
g2=[R("b",0,"The Wanderer"),R("b",1,"The Seafarer"),R("b",2,"Dream of the Rood")]
g3=[R("c",0,"The Seafarer"),R("c",1,"Dream of the Rood"),R("c",2,"Judith")]
layers,_ = segment_and_merge([("a",g1),("b",g2),("c",g3)])
check("single layer", len(layers)==1, f"{len(layers)}")
check("5 unique books", len(layers[0])==5, str([b.title for b in layers[0]]))
check("Seafarer read 3x", layers[0][2].n_reads==3, str(layers[0][2].n_reads))

print("\n=== 3. layer boundary when no overlap ===")
h1=[R("a",0,"Beowulf"),R("a",1,"The Wanderer")]
h2=[R("b",0,"Summa Theologiae"),R("b",1,"Consolation of Philosophy")]
layers,joins = segment_and_merge([("a",h1),("b",h2)])
check("two layers", len(layers)==2, f"{len(layers)}")
check("boundary verdict", joins[0]["verdict"]=="layer_boundary", joins[0]["verdict"])

print("\n=== 4. misread in overlap -> disagreement flagged ===")
m1=[R("a",0,"Piers Plowman"),R("a",1,"Confessio Amantis"),R("a",2,"Morte Darthur")]
m2=[R("b",0,"Confessio Amantis"),R("b",1,"Mort Artu"),R("b",2,"Brut")]
layers,_ = segment_and_merge([("a",m1),("b",m2)])
disag=[b for b in layers[0] if "read_disagreement" in b.flags]
check("disagreement caught", len(disag)==1, str([(b.title,b.flags) for b in layers[0]]))
check("variants recorded", disag and len(disag[0].variants)==2, str(disag[0].variants if disag else None))

print("\n=== 5. illegible in one frame, clear in the other ===")
i1=[R("a",0,"Ancrene Wisse"),R("a",1,None,leg="illegible",edge=True)]
i2=[R("b",0,"Hali Meidhad","Anon",edge=True),R("b",1,"Sawles Warde")]
layers,joins = segment_and_merge([("a",i1),("b",i2)])
check("did NOT merge on illegible", joins[0]["overlap"]==0, str(joins[0]))

i3=[R("a",0,"Ancrene Wisse"),R("a",1,"Hali Meidhad"),R("a",2,None,leg="illegible",edge=True)]
i4=[R("b",0,"Hali Meidhad","Anon"),R("b",1,"Sawles Warde","Anon"),R("b",2,"Katherine Group")]
layers,joins = segment_and_merge([("a",i3),("b",i4)])
hm=[b for b in layers[0] if b.title=="Hali Meidhad"]
check("merged on the legible pair", joins[0]["overlap"]>=1, str(joins[0]))
check("author recovered from other frame", hm and hm[0].author=="Anon", str(hm[0].author if hm else None))

print("\n=== 6. edge read loses to full read ===")
e1=[R("a",0,"Owl and Nightingale"),R("a",1,"Layamon Br",leg="partial",edge=True)]
e2=[R("b",0,"Layamon Brut","Layamon"),R("b",1,"Ormulum")]
layers,_ = segment_and_merge([("a",e1),("b",e2)])
lay=[b for b in layers[0] if b.title and "Layamon" in b.title]
check("full title wins over cut-off", lay and lay[0].title=="Layamon Brut", str(lay[0].title if lay else None))
check("legibility upgraded", lay and lay[0].legibility=="clear", str(lay[0].legibility if lay else None))

print("\n=== 7. weak single-book join is labelled ===")
w1=[R("a",0,"Bede"),R("a",1,"Gildas"),R("a",2,"Nennius")]
w2=[R("b",0,"Nennius"),R("b",1,"Asser"),R("b",2,"Alfred")]
_,joins = segment_and_merge([("a",w1),("b",w2)])
check("k=1 flagged weak", joins[0]["verdict"]=="same_layer_weak_join", str(joins[0]))

print("\n=== 8. single-read books flagged ===")
layers,_ = segment_and_merge([("a",[R("a",0,"Solo Title")])])
check("single_read flag", "single_read" in layers[0][0].flags, str(layers[0][0].flags))

print("\n=== 9. realistic 25-frame run (10 layers, mixed overlaps) ===")
import random; random.seed(7)
A=["Chronicle","History","Life","Letters","Sermons","Poems","Dialogues","Travels",
   "Commentary","Rule","Vision","Romance","Legend","Chronicles","Meditations","Sayings"]
B=["Bede","Alcuin","Aelfric","Wulfstan","Anselm","Bernard","Aquinas","Ockham","Bacon",
   "Grosseteste","Wyclif","Langland","Gower","Lydgate","Hoccleve","Malory","Caxton",
   "Julian","Kempe","Rolle","Hilton","Chaucer","Trevisa","Higden","Walsingham"]
C=["of Durham","of York","of Canterbury","of Winchester","of Ely","of Lincoln","of Bath",
   "of Wells","of Exeter","of Norwich","of Rochester","of Hereford"]
CORPUS=[]
for b in B:
    for a in A:
        for c in C:
            CORPUS.append(f"{a} of {b} {c}")
random.shuffle(CORPUS)
frames=[]; truth=0; c=0
for layer in range(10):
    n_ph = random.choice([2,2,3])
    size = random.randint(9,14)
    books=[CORPUS[c+j] for j in range(size)]; c+=size; truth+=size
    start=0
    for p_i in range(n_ph):
        take = size-start if p_i==n_ph-1 else max(4,(size//n_ph)+random.randint(1,3))
        seg=books[start:start+take]
        nm=f"IMG_{2160+len(frames)}"
        frames.append((nm,[R(nm,i,t) for i,t in enumerate(seg)]))
        start += max(1, take - random.randint(2,3))
        if start>=size: break
layers,joins=segment_and_merge(frames)
total=sum(len(l) for l in layers)
check("layer count", len(layers)==10, f"got {len(layers)}")
check("no duplicate inflation", total==truth, f"merged {total} vs truth {truth}")
print(f"     frames={len(frames)}  layers={len(layers)}  books={total}  truth={truth}")
print(f"     boundaries found: {sum(1 for j in joins if j['verdict']=='layer_boundary')}")

print("\n=== 10. noisy reads (10% OCR corruption) survive stitching ===")
def corrupt(t, rate=0.10):
    out=[]
    for ch in t:
        if random.random()<rate: continue
        out.append(ch)
    return "".join(out) or t
frames2=[]; truth2=0; c=200
for layer in range(4):
    size=random.randint(8,11); books=[CORPUS[c+j] for j in range(size)]; c+=size; truth2+=size
    start=0
    for p_i in range(2):
        take = size-start if p_i==1 else size//2+2
        seg=books[start:start+take]
        nm=f"NZ_{layer}_{p_i}"
        frames2.append((nm,[R(nm,i,corrupt(t)) for i,t in enumerate(seg)]))
        start += max(1, take-2)
layers2,_=segment_and_merge(frames2)
tot2=sum(len(l) for l in layers2)
check("noisy: never loses books", tot2>=truth2, f"merged {tot2} vs truth {truth2}")
check("noisy: within 10% of truth", abs(tot2-truth2)<=max(2,truth2*0.10), f"merged {tot2} vs truth {truth2}")
print(f"     layers={len(layers2)}  books={tot2}  truth={truth2}")

print("\n=== 11. safety net catches missed joins ===")
sn1=[R("a",0,"Historia Ecclesiastica"),R("a",1,"Vita Sancti Cuthberti"),R("a",2,"De Temporum Ratione")]
sn2=[R("b",0,"De Temporum Rati0ne"),R("b",1,"Epistola ad Ecgbertum")]
ly,jn=segment_and_merge([("a",sn1),("b",sn2)])
sus=find_suspect_joins(ly)
check("split into 2 layers (strict pass)", len(ly)==2, f"got {len(ly)}")
check("safety net flags the boundary", len(sus)==1, str(sus))
check("net does not merge", sum(len(l) for l in ly)==5, str(sum(len(l) for l in ly)))

print("\n=== 12. duplicate report finds repeated titles ===")
d1=[R("a",0,"Summa Theologiae"),R("a",1,"Confessions")]
d2=[R("b",0,"Rule of St Benedict"),R("b",1,"Summa Theologiae")]
ly2,_=segment_and_merge([("a",d1),("b",d2)])
dups=duplicate_report(ly2)
check("duplicate copy reported", any(x["title_a"]=="Summa Theologiae" for x in dups), str(dups))

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
