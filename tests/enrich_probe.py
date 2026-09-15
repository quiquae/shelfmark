"""Probe which metadata backends are actually reachable and what they return.
Reports failures honestly rather than assuming availability."""
import json, warnings; warnings.filterwarnings("ignore")
import requests, isbnlib

ISBNS = ["9780199535507", "9780140449082", "9780674430006"]
UA = {"User-Agent": "shelfcat-pilot/0.1 (library cataloguing; contact: creaghfactor@gmail.com)"}

def openlibrary(isbn):
    r = requests.get("https://openlibrary.org/api/books",
                     params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
                     headers=UA, timeout=15)
    r.raise_for_status()
    d = r.json().get(f"ISBN:{isbn}")
    if not d: return None
    return {"title": d.get("title"),
            "authors": [a["name"] for a in d.get("authors", [])],
            "year": (d.get("publish_date") or "")[-4:],
            "publisher": (d.get("publishers") or [{}])[0].get("name")}

def googlebooks(isbn):
    r = requests.get("https://www.googleapis.com/books/v1/volumes",
                     params={"q": f"isbn:{isbn}"}, headers=UA, timeout=15)
    r.raise_for_status()
    items = r.json().get("items") or []
    if not items: return None
    v = items[0]["volumeInfo"]
    return {"title": v.get("title"), "authors": v.get("authors", []),
            "year": (v.get("publishedDate") or "")[:4], "publisher": v.get("publisher")}

for isbn in ISBNS:
    print(f"\n=== {isbn}  valid={isbnlib.is_isbn13(isbn)}  mask={isbnlib.mask(isbn)}")
    for name, fn in [("openlibrary", openlibrary), ("googlebooks", googlebooks)]:
        try:
            out = fn(isbn)
            print(f"  {name:<12} {json.dumps(out, ensure_ascii=False) if out else 'NO RECORD'}")
        except Exception as e:
            print(f"  {name:<12} ERROR {type(e).__name__}: {str(e)[:90]}")
