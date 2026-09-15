"""Empirical check: how do open-source decoders behave on synthetic EAN-13
under the degradations we expect from shelf photography (blur, angle, scale)?"""
import io, warnings
warnings.filterwarnings("ignore")
import numpy as np, cv2
from PIL import Image
import barcode
from barcode.writer import ImageWriter
from pyzbar.pyzbar import decode as zbar_decode
import zxingcpp

ISBNS = ["9780199535507", "9780140449082", "9780674430006"]

def make(isbn):
    ean = barcode.get("ean13", isbn[:-1], writer=ImageWriter())
    buf = io.BytesIO(); ean.write(buf, options={"module_height": 12.0, "quiet_zone": 4.0})
    return cv2.cvtColor(np.array(Image.open(buf).convert("RGB")), cv2.COLOR_RGB2BGR)

def degrade(img, kind):
    h, w = img.shape[:2]
    if kind == "clean":      return img
    if kind == "blur3":      return cv2.GaussianBlur(img, (3, 3), 0)
    if kind == "blur9":      return cv2.GaussianBlur(img, (9, 9), 0)
    if kind == "blur21":     return cv2.GaussianBlur(img, (21, 21), 0)
    if kind == "small_25%":  return cv2.resize(img, (w // 4, h // 4))
    if kind == "small_12%":  return cv2.resize(img, (w // 8, h // 8))
    if kind == "rot15":
        M = cv2.getRotationMatrix2D((w/2, h/2), 15, 1.0)
        return cv2.warpAffine(img, M, (w, h), borderValue=(255,255,255))
    if kind == "rot90":      return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if kind == "lowcontrast":return cv2.convertScaleAbs(img, alpha=0.25, beta=140)
    if kind == "glare":
        o = img.copy(); cv2.ellipse(o, (w//2, h//2), (w//3, h//5), 0, 0, 360, (255,255,255), -1)
        return cv2.addWeighted(img, 0.55, o, 0.45, 0)
    raise ValueError(kind)

def try_zbar(img):
    try:  return [r.data.decode() for r in zbar_decode(img)]
    except Exception: return []

def try_zxing(img):
    try:  return [r.text for r in zxingcpp.read_barcodes(img)]
    except Exception: return []

def try_cv2(img):
    try:
        ok, info, *_ = cv2.barcode.BarcodeDetector().detectAndDecode(img)
        return [i for i in (info or []) if i]
    except Exception: return []

def sharpness(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(g, cv2.CV_64F).var())

KINDS = ["clean","blur3","blur9","blur21","small_25%","small_12%","rot15","rot90","lowcontrast","glare"]
print(f"{'degradation':<14}{'sharpness':>10}{'zbar':>8}{'zxing':>8}{'opencv':>8}{'union':>8}")
print("-" * 56)
tot = {"zbar":0,"zxing":0,"cv2":0,"union":0}
for kind in KINDS:
    hits = {"zbar":0,"zxing":0,"cv2":0,"union":0}; sh = []
    for isbn in ISBNS:
        img = degrade(make(isbn), kind); sh.append(sharpness(img))
        z, x, c = try_zbar(img), try_zxing(img), try_cv2(img)
        hits["zbar"]  += isbn in z
        hits["zxing"] += isbn in x
        hits["cv2"]   += isbn in c
        hits["union"] += isbn in set(z) | set(x) | set(c)
    for k in tot: tot[k] += hits[k]
    print(f"{kind:<14}{np.mean(sh):>10.0f}{hits['zbar']:>7}/3{hits['zxing']:>7}/3{hits['cv2']:>7}/3{hits['union']:>7}/3")
n = len(KINDS) * len(ISBNS)
print("-" * 56)
print(f"{'TOTAL':<14}{'':>10}{tot['zbar']:>7}/{n}{tot['zxing']:>7}/{n}{tot['cv2']:>7}/{n}{tot['union']:>7}/{n}")
