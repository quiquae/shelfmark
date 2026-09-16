"""`shelfmark` — start the server.

One command, sane defaults, and it says out loud whether a vision backend is
configured. A librarian should never have to remember a uvicorn invocation,
and a first run that silently has no way to read a photograph is worse than
one that refuses.
"""
import argparse
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="shelfmark",
        description="Photograph a shelf, get an ordered catalogue.")
    ap.add_argument("--host", default=os.environ.get("SHELFMARK_HOST", "127.0.0.1"),
                    help="default 127.0.0.1; bind 0.0.0.0 only behind a reverse "
                         "proxy that terminates TLS")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("SHELFMARK_PORT", "8031")))
    ap.add_argument("--reload", action="store_true", help="for development")
    ap.add_argument("--fake-vision", action="store_true",
                    help="three hardcoded spines per crop; no API key needed")
    args = ap.parse_args(argv)

    if args.fake_vision:
        os.environ["SHELFCAT_FAKE_VISION"] = "1"

    fake = os.environ.get("SHELFCAT_FAKE_VISION", "").lower() in ("1", "true", "yes", "on")
    if fake:
        backend = "FAKE — three hardcoded spines per crop, no photograph is read"
    else:
        from shelfcat import vision
        if vision.available():
            backend = f"{vision.DEFAULT_MODEL} at effort {vision.DEFAULT_EFFORT}"
        else:
            print("shelfmark: no vision backend and no API key.\n"
                  "  Either  export ANTHROPIC_API_KEY=...   (bills your account,\n"
                  "          about a penny a book)\n"
                  "  or      shelfmark --fake-vision        (to see it work first)",
                  file=sys.stderr)
            return 2

    try:
        import uvicorn
    except ImportError:
        print("shelfmark: uvicorn is missing. pip install -e .", file=sys.stderr)
        return 2

    # flush: uvicorn logs to stderr, so an unflushed banner on stdout arrives
    # after it and the first thing the operator reads is out of order.
    print(f"shelfmark on http://{args.host}:{args.port}", flush=True)
    print(f"  vision: {backend}", flush=True)
    print(f"  data:   {os.environ.get('SHELFMARK_DB', 'work/shelfmark.db')}",
          flush=True)
    uvicorn.run("web.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
