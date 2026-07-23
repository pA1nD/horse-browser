import sys

# Windows default stdout/stderr encoding is cp1252
# which can't encode the 🐴 marker helpers prepend to tab titles (or anything
# else outside the locale charset). Force UTF-8 so `print(page_info())` and
# tracebacks carrying page titles don't UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try: _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception: pass

from .lifecycle import ensure_daemon, restart_daemon
from .helpers import *

HELP = """horse-harness (vendored browser-harness core)

Typical usage:
  horse-browser <<'PY'
  tid = bh_open("https://example.com")
  print(page_info())
  PY

Helpers are pre-imported. The daemon auto-starts and connects to the browser
named by BU_CDP_URL / BU_CDP_WS (horse-browser sets this for you).

Commands:
  --help          this text
  --reload        stop the daemon so next call picks up code changes
  --debug-clicks  annotate click coordinates on debug screenshots
"""

USAGE = """Usage:
  horse-browser <<'PY'
  print(page_info())
  PY
"""


def main():
    _run(sys.argv[1:])


def _run(args):
    if args and args[0] in {"-h", "--help"}:
        print(HELP)
        return
    if args and args[0] == "--reload":
        restart_daemon()
        print("daemon stopped — will restart fresh on next call")
        return
    if args and args[0] == "--debug-clicks":
        import os
        os.environ["BH_DEBUG_CLICKS"] = "1"
        args = args[1:]
    if not args and not sys.stdin.isatty():
        code = sys.stdin.read()
        if not code.strip():
            sys.exit(USAGE)
    else:
        sys.exit(USAGE)
    ensure_daemon()
    exec(code, globals())


if __name__ == "__main__":
    main()
