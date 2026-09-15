import sys, os, io, warnings; warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, cv2
from PIL import Image
import barcode as bc
from barcode.writer import ImageWriter
from shelfcat.barcodes import extract, is_bookland, capture_advice

def render(isbn13, scale=1.0):
    e = bc.get("ean13", isbn13[:-1], writer=ImageWriter())
    buf = io.BytesIO(); e.write(buf, options={"module_height": 12.0, "quiet_zone": 4.0})
    img = cv2.cvtColor(np.array(Image.open(buf).convert("RGB")), cv2.COLOR_RGB2BGR)
    if scale != 1.0:
        h, w = img.shape[:2]
        img = cv2.resize(img, (int(w*scale), int(h*scale)), interpolation=cv2.INTER_AREA)
    return img

print("--- check digit / bookland filter ---")
cases = [("9780199535507", True), ("9780140449082", True), ("9791234567896", None),
         ("5012345678900", False), ("9780199535500", False)]
for v, exp in cases:
    got = is_bookland(v)
    verdict = "PASS" if exp is None or got == exp else "FAIL"
    print(f"  {verdict}  {v} -> bookland={got}" + ("  (expected %s)" % exp if exp is not None else "  (979 range)"))

print("\n--- extraction + quality gate at decreasing scale ---")
for scale, label in [(1.0,"full"), (0.65,"~300px"), (0.45,"~210px"), (0.3,"~140px")]:
    img = render("9780199535507", scale)
    hits = extract(img)
    if hits:
        h = hits[0]
        print(f"  {label:<8} w={img.shape[1]:>4}px  decoded={h.value}  "
              f"bc_px={h.px_width:>3}  ppm={h.px_per_module:<5} q={h.quality:<9} "
              f"detectors={'+'.join(h.detectors)}")
    else:
        print(f"  {label:<8} w={img.shape[1]:>4}px  NO DECODE  (correctly reports nothing rather than guessing)")

print("\n--- two barcodes in one frame (batch capture) ---")
a, b = render("9780199535507"), render("9780140449082")
h = max(a.shape[0], b.shape[0])
pad = lambda im: cv2.copyMakeBorder(im, 0, h-im.shape[0], 20, 20, cv2.BORDER_CONSTANT, value=(255,255,255))
combo = np.hstack([pad(a), pad(b)])
hits = extract(combo)
print(f"  frame {combo.shape[1]}x{combo.shape[0]} -> {len(hits)} hits: {[x.value for x in hits]}")

print("\n--- capture advice (turn px/module floor into a framing rule) ---")
for mp, wpx in [("12MP phone", 4032), ("48MP phone (full-res)", 8000)]:
    for fw in (300, 400, 600):
        a = capture_advice(wpx, fw)
        print(f"  {mp:<22} frame {fw}mm -> barcode ~{a['expected_barcode_px']}px  {a['verdict']}")
    print(f"  {'':<22} max safe frame width: {capture_advice(wpx,400)['max_frame_width_mm_for_safe']}mm\n")
