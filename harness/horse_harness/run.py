import sys

# Windows default stdout/stderr encoding is cp1252
# which can't encode the 🐴 marker helpers prepend to tab titles (or anything
# else outside the locale charset). Force UTF-8 so `print(page_info())` and
# tracebacks carrying page titles don't UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try: _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception: pass

from .lifecycle import ensure_daemon, restart_daemon, daemon_alive
from .helpers import *
from .input import *


def _load_agent_helpers():
    """Load the operator's workspace agent_helpers.py into this namespace.

    The workspace file was written against the ambient-namespace world (it calls
    cdp/type_into/bh_open without imports, and may `import browser_harness.*`), so:
    seed its namespace with every public helper first, alias browser_harness ->
    horse_harness in sys.modules, and never let a broken workspace file take down
    the CLI — warn and continue instead."""
    from .helpers import AGENT_WORKSPACE
    p = AGENT_WORKSPACE / "agent_helpers.py"
    if not p.exists():
        return
    import types
    import horse_harness
    from . import helpers as _helpers_mod, _ipc as _ipc_mod
    sys.modules.setdefault("browser_harness", horse_harness)
    sys.modules.setdefault("browser_harness.helpers", _helpers_mod)
    sys.modules.setdefault("browser_harness._ipc", _ipc_mod)
    module = types.ModuleType("horse_harness_agent_helpers")
    module.__file__ = str(p)
    module.__dict__.update({n: v for n, v in globals().items() if not n.startswith("_")})
    try:
        exec(compile(p.read_text(encoding="utf-8"), str(p), "exec"), module.__dict__)
    except Exception as e:
        print(f"horse-harness: workspace agent_helpers.py failed to load ({e!r}) — "
              f"fix or remove {p}", file=sys.stderr)
        return
    for name, value in vars(module).items():
        if not name.startswith("_"):
            globals()[name] = value


_load_agent_helpers()

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
  --doctor        diagnose endpoint, daemon, and extension state
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


def _doctor():
    import os, urllib.request
    url = os.environ.get("BU_CDP_URL") or ""
    ws = os.environ.get("BU_CDP_WS") or ""
    print(f"endpoint: {url or ws or 'UNSET (invoke via horse-browser)'}")
    if url:
        try:
            urllib.request.urlopen(f"{url.rstrip('/')}/json/version", timeout=3).close()
            print("browser:  reachable")
        except Exception as e:
            print(f"browser:  UNREACHABLE ({e}) — run `horse-browser` to launch/heal it")
            return 1
    print(f"daemon:   {'alive' if daemon_alive() else 'down (auto-starts on next script)'}")
    try:
        sw = next((t for t in cdp("Target.getTargets")["targetInfos"]
                   if t.get("type") == "service_worker"
                   and t.get("url", "").startswith("chrome-extension://")), None)
        print(f"extension: {'live' if sw else 'NOT FOUND (tab grouping + focus-safe activate degraded)'}")
    except Exception as e:
        print(f"extension: could not query ({e})")
    return 0


def _run(args):
    if args and args[0] in {"-h", "--help"}:
        print(HELP)
        return
    if args and args[0] in {"--doctor", "doctor"}:
        sys.exit(_doctor())
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
