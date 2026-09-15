"""Find the minimum on-image barcode width (px) that still decodes, and
check whether variance-of-Laplacian predicts that failure. Also verify the
OpenCV BarcodeDetector API rather than assuming it is broken."""
import io, warnings; warnings.filterwarnings("ignore")
import numpy as np, cv2
from PIL import Image
import barcode
from barcode.writer import ImageWriter
from pyzbar.pyzbar import decode as zbar_decode
import zxingcpp

ISBN = "9780199535507"
ean = barcode.get("ean13", ISBN[:-1], writer=ImageWriter())
buf = io.BytesIO(); ean.write(buf, options={"module_height": 12.0, "quiet_zone": 4.0})
base = cv2.cvtColor(np.array(Image.open(buf).convert("RGB")), cv2.COLOR_RGB2BGR)
H, W = base.shape[:2]
print(f"rendered barcode: {W}x{H} px  (EAN-13 = 95 modules wide + quiet zones)\n")

def sharp(i): return float(cv2.Laplacian(cv2.cvtColor(i, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())

print(f"{'width_px':>9}{'px/module':>11}{'sharpness':>11}{'zbar':>7}{'zxing':>7}")
print("-"*45)
for w in [1200, 800, 600, 500, 400, 350, 300, 250, 200, 150, 120, 100, 80]:
    img = cv2.resize(base, (w, max(1, int(H * w / W))), interpolation=cv2.INTER_AREA)
    z = ISBN in [r.data.decode() for r in zbar_decode(img)]
    try: x = ISBN in [r.text for r in zxingcpp.read_barcodes(img)]
    except Exception: x = False
    print(f"{w:>9}{w/95:>11.2f}{sharp(img):>11.0f}{'PASS' if z else 'fail':>7}{'PASS' if x else 'fail':>7}")

print("\n--- OpenCV BarcodeDetector API check ---")
d = cv2.barcode.BarcodeDetector()
out = d.detectAndDecode(base)
print("returns tuple of len", len(out), "->", [type(o).__name__ for o in out])
print("decoded_info:", out[0] if isinstance(out[0], (list, tuple)) else out)
