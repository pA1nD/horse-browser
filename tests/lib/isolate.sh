# tests/lib/isolate.sh — give a test suite a DISPOSABLE horse-browser instance.
#
# The shell suites hard-kill, stampede-launch, and relaunch the browser. Run against the live
# :9223 instance that the operator and other agents share, that is destructive. This bootstrap
# points the suite at its OWN browser — a free port + a throwaway profile + (via the launcher's
# per-profile derivation) its own single-flight lock and daemon — so the live browser is never
# touched. Teardown kills the isolated Chrome + daemon and removes the temp dir on exit.
#
# Usage: after $HB is resolved, `source "$HERE/lib/isolate.sh"; hb_isolate`. Then read PORT from
# $HORSE_BROWSER_PORT and the lock from $HB_LOCK_PATH, and call `_hb_isolate_teardown` from the
# suite's own cleanup/trap (every suite sets its own EXIT trap after this, which would clobber a
# trap set here — so teardown is caller-driven). Escape hatch: HB_ISOLATE=0 runs against the
# live/config instance (the old behaviour); _hb_isolate_teardown is then a safe no-op.

_hb_free_port() {
  python3 - <<'PY' 2>/dev/null
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()
PY
}

# Resolve a Chrome binary WITHOUT depending on the machine's horse-browser setup, so the
# suites run on a box where horse-browser was never installed (CI, shipmate, a fresh clone).
# Order: HORSE_BROWSER_BIN (explicit) → the user's config (fast path, no download) → a
# test-owned cache at ~/.cache/horse-browser-tests, fetched once via @puppeteer/browsers
# and reused forever. The extension needs nothing: the launcher falls back to the repo's own.
_hb_test_browser() {
  [ -n "${HORSE_BROWSER_BIN:-}" ] && return 0
  local cfg="$HOME/.config/horse-browser/config" bin
  bin="$(sed -n 's/^BROWSER_BIN=//p' "$cfg" 2>/dev/null | tr -d '"' | head -1)"
  [ -x "$bin" ] && return 0   # launcher will read the config itself
  local cache="${HB_TEST_BROWSER_CACHE:-$HOME/.cache/horse-browser-tests}"
  bin="$(ls -d "$cache"/chrome/*/chrome-*/"Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" 2>/dev/null | sort | tail -1)"
  if [ ! -x "$bin" ]; then
    command -v npx >/dev/null 2>&1 || { echo "isolate: no configured browser and no npx to fetch one — set HORSE_BROWSER_BIN" >&2; return 1; }
    echo "isolate: no configured browser — fetching Chrome for Testing into $cache (one-time, ~170MB)…" >&2
    local out; out="$(npx -y @puppeteer/browsers install chrome@stable --path "$cache")" || return 1
    bin="$(printf '%s\n' "$out" | grep '^chrome@' | tail -1 | sed 's/^[^ ]* //')"
    [ -x "$bin" ] || { echo "isolate: Chrome fetch did not yield an executable" >&2; return 1; }
  fi
  export HORSE_BROWSER_BIN="$bin"
  echo "isolate: using test-cache browser ($bin)" >&2
}

hb_isolate() {
  if [ "${HB_ISOLATE:-1}" = "0" ]; then
    echo "isolate: DISABLED (HB_ISOLATE=0) — running against the live/config instance" >&2
    return 0
  fi
  _hb_test_browser || return 1
  local port; port="$(_hb_free_port)"
  [ -n "$port" ] && [ "$port" != 0 ] || { echo "isolate: could not find a free port" >&2; return 1; }
  HB_ISO_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/hb-iso.XXXXXX")"
  export HORSE_BROWSER_PORT="$port"
  export HORSE_BROWSER_PROFILE="$HB_ISO_ROOT/profile"
  # A UNIQUE session identity is essential: the per-session daemon is keyed by BU_NAME (derived
  # from the session), and the _ipc singleton lock allows one daemon per BU_NAME. If we inherit
  # the operator's CLAUDE_CODE_SESSION_ID, the suite reuses their LIVE daemon — pinned to :9223 —
  # and the port override is silently defeated (the suite then drives the live browser). Give the
  # isolated instance its own session so it spawns its own daemon on our port.
  # Same trap, wider: ANY inherited daemon-identity/endpoint var silently reroutes the
  # suite to the live daemon (an explicit BU_NAME beats the derivation; BU_CDP_WS beats
  # BU_CDP_URL in the daemon). Shed them all before setting our own identity.
  unset CLAUDE_CODE_SESSION_ID BU_NAME BU_CDP_WS BU_CDP_URL HORSE_LANE BH_ANCHOR_PID BH_ANCHOR_START
  export HORSE_SESSION="hbtest-$$-$port"
  # Always test THIS checkout's extension. Without this, a valid user config wins and the
  # suite silently drives (and version-syncs!) the operator's installed npm extension.
  local _repo; _repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  [ -d "$_repo/extension" ] && export HORSE_BROWSER_EXTENSION="$_repo/extension"
  export HB_ISOLATED=1   # suites gate non-hermetic checks (external network) on this
  HB_LOCK_PATH="$HB_ISO_ROOT/profile.browser-lock"   # matches the launcher's ${PROFILE%/}.browser-lock
  echo "isolate: dedicated horse-browser on :$port  (profile $HORSE_BROWSER_PROFILE)"
}

_hb_isolate_teardown() {
  [ -n "${HB_ISO_ROOT:-}" ] || return 0
  # Kill the isolated daemon (its env carries our BU_CDP_URL) then its Chrome (unique
  # --user-data-dir). Both keys are unique to this instance, so the live browser is never hit.
  for p in $(pgrep -f "horse_harness.daemon" 2>/dev/null || true); do
    ps eww -o command= -p "$p" 2>/dev/null | grep -q "BU_CDP_URL=http://127.0.0.1:$HORSE_BROWSER_PORT" \
      && kill "$p" 2>/dev/null || true
  done
  pkill -f -- "--user-data-dir=$HORSE_BROWSER_PROFILE" 2>/dev/null || true
  # let Chrome release the profile dir before removing it, else rm races a live process and leaks
  for _ in 1 2 3 4 5; do pgrep -f -- "--user-data-dir=$HORSE_BROWSER_PROFILE" >/dev/null 2>&1 || break; sleep 0.4; done
  rm -rf "$HB_ISO_ROOT" 2>/dev/null || true
}
