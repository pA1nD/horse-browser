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


# Three tiers of verbs, loaded lowest-precedence first (later wins on a name clash):
#   core     — helpers.py (the wildcard import above)
#   plugins  — <workspace>/plugins/*.py, sorted
#   local    — <workspace>/agent_helpers.py, LAST, so the operator's own file always wins.
# The broker's security lives in the daemon (origin-checked, policy-gated at the socket), not in the
# Python verb, so letting local shadow anything is safe — worst case it breaks its own convenience.
# NB precedence is ENTRY-POINT scoped: overriding e.g. `cdp` changes what the agent's script calls,
# but a core verb that calls `cdp` internally still uses helpers.py's own. `verbs --json` shows shadows.
_VERB_SOURCES = {}   # name -> "core" | "plugin:<file>" | "local"
_VERB_FILES = {}     # name -> absolute source path
_VERB_SHADOWS = {}   # name -> [tiers it shadowed], so overriding a core/plugin verb is visible


def _exec_into_globals(path, tier):
    """Exec a workspace/plugin file in a namespace seeded with the CURRENT globals (so each file
    sees core + everything loaded before it), then copy its public names back — later files win.
    Never raise: a broken file warns to stderr and is skipped, never taking down the CLI."""
    import types
    module = types.ModuleType("horse_harness_ext")
    module.__file__ = str(path)
    module.__dict__.update({n: v for n, v in globals().items() if not n.startswith("_")})
    seeded = dict(module.__dict__)   # snapshot the seed so we can tell what THIS file introduces
    try:
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    except Exception as e:
        print(f"horse-harness: {path.name} failed to load ({e!r}) — fix or remove {path}",
              file=sys.stderr)
        return
    for name, value in vars(module).items():
        if name.startswith("_") or (name in seeded and seeded[name] is value):
            continue                 # unchanged seed (a core verb / stdlib import) — leave its tier
        globals()[name] = value
        if getattr(value, "__code__", None) is None or getattr(value, "_renamed_to", None):
            continue   # not a verb (import/class/const), or a deprecated alias — callable but not listed
        if name in _VERB_SOURCES and _VERB_SOURCES[name] != tier:
            _VERB_SHADOWS.setdefault(name, []).append(_VERB_SOURCES[name])   # this file shadows an earlier tier
        _VERB_SOURCES[name] = tier   # tags functions defined here AND re-exports/aliases (screenshot = capture_screenshot)
        _VERB_FILES[name] = str(path)


def _load_workspace():
    """Load the plugin tier then the local tier onto the core verbs already imported.

    The files were written against the ambient-namespace world (they call cdp/type_into/open_tab
    without imports, and may `import browser_harness.*`), so alias browser_harness -> horse_harness
    in sys.modules; each file's namespace is seeded with every public helper by _exec_into_globals."""
    from .helpers import AGENT_WORKSPACE
    import horse_harness
    from . import helpers as _helpers_mod, _ipc as _ipc_mod
    sys.modules.setdefault("browser_harness", horse_harness)
    sys.modules.setdefault("browser_harness.helpers", _helpers_mod)
    sys.modules.setdefault("browser_harness._ipc", _ipc_mod)
    # tag the core verbs already present from the two wildcard imports above
    for name, value in list(globals().items()):
        if name.startswith("_") or not callable(value):
            continue
        if getattr(value, "__module__", "") == "horse_harness.helpers" and not getattr(value, "_renamed_to", None):
            _VERB_SOURCES.setdefault(name, "core")   # skip deprecated aliases — they warn+forward, not listed
            _VERB_FILES.setdefault(name, getattr(sys.modules.get(value.__module__), "__file__", "") or "")
    plugins = AGENT_WORKSPACE / "plugins"
    if plugins.is_dir():
        for p in sorted(plugins.glob("*.py")):
            _exec_into_globals(p, f"plugin:{p.name}")
    local = AGENT_WORKSPACE / "agent_helpers.py"
    if local.exists():
        _exec_into_globals(local, "local")


_load_workspace()

HELP = """horse-harness (vendored browser-harness core)

Typical usage:
  horse-browser <<'PY'
  tid = open_tab("https://example.com")
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


def _print_verbs(as_json):
    """`horse-browser verbs [--json]` — every loaded verb with its tier + first docstring line."""
    import json, inspect
    rows = []
    for name in sorted(_VERB_SOURCES):
        fn = globals().get(name)
        if not callable(fn):
            continue
        try:
            sig = str(inspect.signature(fn))
        except (ValueError, TypeError):
            sig = "(...)"
        doc = (inspect.getdoc(fn) or "").strip().splitlines()
        rows.append({"name": name, "tier": _VERB_SOURCES[name], "file": _VERB_FILES.get(name, ""),
                     "signature": sig, "doc": doc[0].strip() if doc else "",
                     "shadows": _VERB_SHADOWS.get(name, [])})
    if as_json:
        print(json.dumps(rows))
    else:
        for r in rows:
            print(f"{r['tier']:20} {r['name']}{r['signature']}")


def _run(args):
    if args and args[0] in {"-h", "--help"}:
        print(HELP)
        return
    if args and args[0] == "verbs":
        _print_verbs("--json" in args)
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
