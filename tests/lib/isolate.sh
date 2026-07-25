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

hb_isolate() {
  if [ "${HB_ISOLATE:-1}" = "0" ]; then
    echo "isolate: DISABLED (HB_ISOLATE=0) — running against the live/config instance" >&2
    return 0
  fi
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
  unset CLAUDE_CODE_SESSION_ID
  export HORSE_SESSION="hbtest-$$-$port"
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
