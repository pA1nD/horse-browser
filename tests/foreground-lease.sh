#!/usr/bin/env bash
# tests/foreground-lease.sh — the foreground lease: taken to drive, dropped when idle.
#
# Emulation.setFocusEmulationEnabled is what lets this tool drive a tab nobody is looking at:
# it makes a background tab report itself visible and focused, so pages behave normally and a
# never-focused tab doesn't read as a bot. It used to be set once by switch_tab and never
# cleared, which meant every tab an agent had ever touched kept COMPOSITING for the daemon's
# whole life. Measured cost of that leak on one browser: two long-idle dashboards burning
# ~40% of a core between them, around the clock, with the browser window not even focused.
#
# So it is a lease. This test pins all four edges of it:
#   taken     — a driven tab reports visible+focused and wears the 🐴
#   dropped   — after HORSE_BROWSER_FOCUS_TTL of silence, both go away (the tab stops painting)
#   retaken   — the very next call that drives the page gets it back, transparently
#   not-idle  — listing tabs is not driving, and must not renew a lease
#
# Every assertion is read over its OWN raw CDP connection, never through the harness: a read
# issued via the helpers would renew the lease it is trying to observe.
set -u

# A test run must never reach the operator's ~/.claude or ~/.grok. 16 of 19 suites once
# lacked this, so `npm test` from ANY clone wired that clone's path into the real global
# settings.json — which is how a build agent's throwaway checkout came to leave a dead
# hook behind that failed every Bash call on the machine. external-state.sh is the one
# suite that unsets this, against temp paths of its own.
export HORSE_BROWSER_NO_RECONCILE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
HB="${HB:-$ROOT/bin/horse-browser}"
[ -x "$HB" ] || HB="$(command -v horse-browser || true)"
[ -x "$HB" ] || { echo "FATAL: horse-browser not found (set HB=…)"; exit 1; }
PY="$ROOT/harness/.venv/bin/python"
[ -x "$PY" ] || { echo "FATAL: harness venv missing"; exit 1; }

# Short TTL so the test doesn't sit for a minute. Exported BEFORE the first horse-browser call:
# the daemon freezes its environment at spawn, so a later export would be read by nobody.
export HORSE_BROWSER_FOCUS_TTL=4
IDLE_WAIT=14          # > TTL + the sweeper's 10s tick

source "$HERE/lib/isolate.sh"; hb_isolate || exit 1
PORT="${HORSE_BROWSER_PORT:-$(sed -n 's/^PORT=//p' "$HOME/.config/horse-browser/config" 2>/dev/null | head -1 | tr -d '"')}"
PORT="${PORT:-9223}"
export HORSE_SESSION="leasetest-$$"

PASS=0; FAIL=0; FAILED=()
say()  { printf '%s\n' "$*"; }
pass() { PASS=$((PASS+1)); say "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); FAILED+=("$1"); say "  ✗ $1${2:+ — $2}"; }

cleanup() {
  for p in $(pgrep -f "horse_harness.daemon" 2>/dev/null); do
    ps eww -o command= -p "$p" 2>/dev/null | tr ' ' '\n' \
      | grep -qE "^HORSE_SESSION=leasetest-$$" && kill "$p" 2>/dev/null
  done
  _hb_isolate_teardown
}
trap cleanup EXIT

"$HB" >/dev/null 2>&1 || { say "FATAL: browser did not come up"; exit 1; }

# ── out-of-band reader ───────────────────────────────────────────────────────
# Attaches its own CDP session to the target and reports the page's own view of itself.
# Focus emulation is applied to the PAGE (any session observes it), while this reader's
# traffic never reaches the daemon — so observing the lease cannot renew it.
cat > "$PWD/.lease-probe.py" <<'PYEOF'
import asyncio, json, sys, urllib.request, websockets

PORT, TID = sys.argv[1], sys.argv[2]
EXPR = "JSON.stringify({hidden:document.hidden,focus:document.hasFocus(),horse:document.title.startsWith('\U0001F434')})"

async def main():
    url = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=5)
                     .read())["webSocketDebuggerUrl"]
    async with websockets.connect(url, max_size=None, ping_interval=None) as ws:
        n = [0]
        async def call(method, params=None, sid=None):
            n[0] += 1; i = n[0]
            m = {"id": i, "method": method, "params": params or {}}
            if sid: m["sessionId"] = sid
            await ws.send(json.dumps(m))
            while True:
                r = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if r.get("id") == i: return r.get("result", {})
        sid = (await call("Target.attachToTarget", {"targetId": TID, "flatten": True}))["sessionId"]
        r = await call("Runtime.evaluate", {"expression": EXPR, "returnByValue": True}, sid)
        await call("Target.detachFromTarget", {"sessionId": sid})
        print(r["result"]["value"])
asyncio.run(main())
PYEOF
probe() { "$PY" "$PWD/.lease-probe.py" "$PORT" "$1" 2>/dev/null; }

say "foreground lease (TTL=${HORSE_BROWSER_FOCUS_TTL}s)"

TID="$("$HB" <<'PY' 2>/dev/null | tail -1
tid = open_tab("data:text/html,<title>lease</title><body>lease</body>")
print(tid)
PY
)"
[ -n "$TID" ] || { say "FATAL: could not open a tab"; exit 1; }

# ── 1. taken ────────────────────────────────────────────────────────────────
S="$(probe "$TID")"
case "$S" in
  *'"hidden":false'*) pass "driven tab reports visible" ;;
  *) fail "driven tab reports visible" "$S" ;;
esac
case "$S" in
  *'"focus":true'*) pass "driven tab reports focused" ;;
  *) fail "driven tab reports focused" "$S" ;;
esac
case "$S" in
  *'"horse":true'*) pass "driven tab wears the horse" ;;
  *) fail "driven tab wears the horse" "$S" ;;
esac

# ── 2. handover: the tab you walk away from is released at once ─────────────
# Opening another tab rebinds the session, which must release the lease on the old one
# rather than leaving it behind — the leak in its purest form. Visibility is NOT asserted
# here: open_tab deliberately doesn't steal the window's active tab, so the tab we just left
# is still the visible one and document.hidden:false is the honest answer for it. The
# painting consequence is asserted below, on a tab that really is in the background.
TID2="$("$HB" <<'PY' 2>/dev/null | tail -1
tid = open_tab("data:text/html,<title>second</title><body>second</body>")
print(tid)
PY
)"
[ -n "$TID2" ] || { say "FATAL: could not open the second tab"; exit 1; }
S="$(probe "$TID")"
case "$S" in
  *'"focus":false'*) pass "tab left behind loses emulated focus" ;;
  *) fail "tab left behind loses emulated focus" "$S" ;;
esac
case "$S" in
  *'"horse":false'*) pass "tab left behind loses the horse" ;;
  *) fail "tab left behind loses the horse" "$S" ;;
esac

# ── 3. dropped on idle, and listing tabs is not driving ─────────────────────
# Browser-level Target.* calls carry no session and must not renew a lease. If they did, any
# polling client (the monitor, a status line) would pin every tab it ever enumerated.
sleep 2
"$HB" <<'PY' >/dev/null 2>&1
list_tabs()
PY
sleep "$IDLE_WAIT"
# TID2 was opened in the background and never made visible, so this is the tab whose
# document.hidden actually reports whether the browser will paint it — the battery claim.
S="$(probe "$TID2")"
case "$S" in
  *'"hidden":true'*) pass "idle background tab goes hidden (browser stops painting it)" ;;
  *) fail "idle background tab goes hidden (browser stops painting it)" "$S" ;;
esac
case "$S" in
  *'"focus":false'*) pass "lease lapses on idle despite list_tabs (browser-level calls don't renew)" ;;
  *) fail "lease lapses on idle despite list_tabs (browser-level calls don't renew)" "$S" ;;
esac
case "$S" in
  *'"horse":false'*) pass "idle tab loses the horse (the mark tracks the lease)" ;;
  *) fail "idle tab loses the horse (the mark tracks the lease)" "$S" ;;
esac

# ── 4. retaken ──────────────────────────────────────────────────────────────
# The point of the whole design: reacquisition is transparent. One ordinary call, and the
# page it lands on must already believe it is foreground — the value below is read BY the
# page during that same call, so a lease taken too late cannot pass this.
SEEN="$("$HB" <<'PY' 2>/dev/null | tail -1
print(js("JSON.stringify({hidden:document.hidden,focus:document.hasFocus()})"))
PY
)"
case "$SEEN" in
  *'"hidden":false'*) pass "next call sees the page already visible (lease retaken in time)" ;;
  *) fail "next call sees the page already visible (lease retaken in time)" "$SEEN" ;;
esac
case "$SEEN" in
  *'"focus":true'*) pass "next call sees the page already focused" ;;
  *) fail "next call sees the page already focused" "$SEEN" ;;
esac
S="$(probe "$TID2")"
case "$S" in
  *'"horse":true'*) pass "horse returns with the lease" ;;
  *) fail "horse returns with the lease" "$S" ;;
esac

rm -f "$PWD/.lease-probe.py"
say ""
say "foreground-lease: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || { for f in "${FAILED[@]}"; do say "  ✗ $f"; done; exit 1; }
