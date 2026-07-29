#!/usr/bin/env bash
# tests/multi-instance.sh — two horse-browsers at once, and neither can see the other.
#
# The normal state of this machine is SEVERAL browsers running side by side, one per agent,
# each with its own --remote-debugging-port and profile. Anything that hardcodes a port or
# writes shared state silently crosses the wires: the symptom that started this was the
# in-browser Monitor talking to :9223 from a browser that wasn't on :9223 — its sidebar
# (chrome.* APIs) showed the right tabs while its wall screencast another agent's.
#
# So this suite runs TWO disposable instances concurrently and asserts each one only ever
# sees itself. It manages both instances itself (isolate.sh's hb_isolate is single-instance),
# and borrows only its hermetic browser resolution — so it runs with no horse-browser install.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
HB="${HB:-$REPO/bin/horse-browser}"
[ -x "$HB" ] || { echo "FATAL: horse-browser not found at $HB"; exit 1; }
source "$HERE/lib/isolate.sh"
_hb_test_browser || exit 1

# Same trap as isolate.sh: an inherited daemon identity/endpoint reroutes us to the operator's
# live daemon and quietly defeats both port overrides.
unset CLAUDE_CODE_SESSION_ID BU_NAME BU_CDP_WS BU_CDP_URL HORSE_LANE BH_ANCHOR_PID BH_ANCHOR_START
export HORSE_BROWSER_EXTENSION="$REPO/extension"   # always test THIS checkout's extension

PASS=0; FAIL=0; FAILED=()
say()  { printf '%s\n' "$*"; }
pass() { PASS=$((PASS+1)); say "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); FAILED+=("$1"); say "  ✗ $1${2:+ — $2}"; }

ROOT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hb-multi.XXXXXX")"
PORT_A="$(_hb_free_port)"; PORT_B="$(_hb_free_port)"
PROF_A="$ROOT_DIR/a"; PROF_B="$ROOT_DIR/b"
PAGE_A="$ROOT_DIR/a.html"; PAGE_B="$ROOT_DIR/b.html"
printf '<title>instance A</title><h1>A</h1>' > "$PAGE_A"
printf '<title>instance B</title><h1>B</h1>' > "$PAGE_B"

# Every session this suite creates carries the SID stem, and BU_NAME derives from the text
# after the session's last dash — so both are unique to this run. Cleanup keys on that, never
# on the port: _hb_free_port binds-then-closes, so a port can belong to somebody else by the
# time we tear down, and killing by port would take out another instance's daemon.
SID="hbmulti-$$"
cleanup() {
  for p in $(pgrep -f "horse_harness.daemon" 2>/dev/null || true); do
    ps eww -o command= -p "$p" 2>/dev/null | tr ' ' '\n' | grep -q "^HORSE_SESSION=$SID-" \
      && kill "$p" 2>/dev/null
  done
  for prof in "$PROF_A" "$PROF_B"; do
    pkill -f -- "--user-data-dir=$prof" 2>/dev/null || true
  done
  sleep 1
  rm -rf "$ROOT_DIR"
}
trap cleanup EXIT

# run <port> <profile> <script…>: one call to a specific instance, with its own session id.
run() {
  local port="$1" prof="$2"; shift 2
  HORSE_BROWSER_PORT="$port" HORSE_BROWSER_PROFILE="$prof" \
  HORSE_SESSION="$SID-x$$p$port" "$HB" "$@"
}
# sw <port> <expr>: evaluate in that instance's extension service worker.
sw() { python3 "$REPO/tools/sw_eval.py" "$1" "$2" 5 2>/dev/null; }
# monitor <port> <expr>: evaluate in that instance's Monitor page.
monitor() {
  python3 - "$1" "$REPO/tools" "$2" <<'PY' 2>/dev/null
import json, sys
sys.path.insert(0, sys.argv[2])
import sw_eval as S
port, expr = sys.argv[1], sys.argv[3]
t = next((t for t in S.http_json(port, "/json/list")
          if "monitor.html" in t.get("url", "") and t.get("webSocketDebuggerUrl")), None)
if not t:
    sys.exit(1)
s = S.ws_connect(port, t["webSocketDebuggerUrl"])
S.ws_send(s, json.dumps({"id": 1, "method": "Runtime.evaluate",
                         "params": {"expression": expr, "awaitPromise": True, "returnByValue": True}}))
s.settimeout(8)
for _ in range(50):
    m = json.loads(S.ws_recv(s))
    if m.get("id") == 1:
        v = ((m.get("result") or {}).get("result") or {}).get("value")
        print(v if isinstance(v, str) else json.dumps(v))
        break
PY
}

say "== multi-instance =="
say "  A :$PORT_A  ($PROF_A)"
say "  B :$PORT_B  ($PROF_B)"

# ── [1] both come up, concurrently, each on its own port ─────────────────────
run "$PORT_A" "$PROF_A" <<<"open_tab('file://$PAGE_A')" >/dev/null 2>&1 &
pid_a=$!
run "$PORT_B" "$PROF_B" <<<"open_tab('file://$PAGE_B')" >/dev/null 2>&1 &
pid_b=$!
wait $pid_a; rc_a=$?
wait $pid_b; rc_b=$?
up() { curl -s -m 3 "http://127.0.0.1:$1/json/version" >/dev/null 2>&1; }
if [ "$rc_a" = 0 ] && [ "$rc_b" = 0 ] && up "$PORT_A" && up "$PORT_B"; then
  pass "two instances launched concurrently, both serving CDP"
else
  fail "two instances launched concurrently, both serving CDP" "rc=$rc_a/$rc_b"
  say ""; say "FATAL: cannot continue without both instances"; exit 1
fi

# ── [2] each extension knows its OWN port ────────────────────────────────────
seed_a="$(sw "$PORT_A" "chrome.storage.local.get('hbCdpPort').then(o=>String(o.hbCdpPort))")"
seed_b="$(sw "$PORT_B" "chrome.storage.local.get('hbCdpPort').then(o=>String(o.hbCdpPort))")"
if [ "$seed_a" = "$PORT_A" ] && [ "$seed_b" = "$PORT_B" ]; then
  pass "launcher seeded each extension with its own debug port"
else
  fail "launcher seeded each extension with its own debug port" "A=$seed_a B=$seed_b"
fi

# ── [3] each Monitor resolved + connected to its own port ────────────────────
# Poll, don't sleep: the Monitor resolves, connects and attaches on its own clock, and a fixed
# wait is exactly what goes flaky on a loaded machine.
want_a="{\"cdp\":\"http://127.0.0.1:$PORT_A\",\"ws\":1}"
want_b="{\"cdp\":\"http://127.0.0.1:$PORT_B\",\"ws\":1}"
st_a=""; st_b=""
for _ in $(seq 1 30); do
  st_a="$(monitor "$PORT_A" "JSON.stringify({cdp:CDP,ws:ws?ws.readyState:null})")"
  st_b="$(monitor "$PORT_B" "JSON.stringify({cdp:CDP,ws:ws?ws.readyState:null})")"
  [ "$st_a" = "$want_a" ] && [ "$st_b" = "$want_b" ] && break
  sleep 1
done
if [ "$st_a" = "$want_a" ] && [ "$st_b" = "$want_b" ]; then
  pass "each Monitor connected to its own browser's port"
else
  fail "each Monitor connected to its own browser's port" "A=$st_a B=$st_b"
fi

# ── [4] the WALL's own socket is attached to its own browser ─────────────────
# Deliberately NOT discover(): that reads chrome.tabs, which is always own-browser, so it
# stays green even with the wall's websocket pointed at a sibling — precisely the split-brain
# this release fixes. Ask over `ws`, the socket the screencast actually runs on.
q='send("Target.getTargets",{}).then(r=>JSON.stringify(((r.result||{}).targetInfos||[]).map(t=>t.url)))'
seen_a="$(monitor "$PORT_A" "$q")"
seen_b="$(monitor "$PORT_B" "$q")"
if [[ "$seen_a" == *"a.html"* && "$seen_a" != *"b.html"* \
   && "$seen_b" == *"b.html"* && "$seen_b" != *"a.html"* ]]; then
  pass "each Monitor's wall socket sees only its own instance's tabs"
else
  fail "each Monitor's wall socket sees only its own instance's tabs" "A=$seen_a B=$seen_b"
fi

# ── [5] one session pointed at a second browser gets a daemon on THAT browser ─
# The harness reuses a daemon by session NAME, and a daemon's CDP endpoint is frozen at
# spawn — so without the endpoint guard the second call here keeps driving instance A
# while the caller believes it is driving B. (This is the footgun isolate.sh sidesteps by
# shedding the inherited session id; the guard makes it correct instead of merely avoided.)
printf '<title>shared 1</title>' > "$ROOT_DIR/s1.html"
printf '<title>shared 2</title>' > "$ROOT_DIR/s2.html"
# BU_NAME is derived from the text after the session's LAST dash, so the tail carries the pid
# too — a bare "…-shared" would make every run claim the daemon named hb-shared, and a bare
# port would collide with any unrelated daemon whose session happens to end in that number.
SHARED="$SID-x$$s$PORT_A"
HORSE_BROWSER_PORT="$PORT_A" HORSE_BROWSER_PROFILE="$PROF_A" HORSE_SESSION="$SHARED" \
  "$HB" <<<"open_tab('file://$ROOT_DIR/s1.html')" >/dev/null 2>&1
HORSE_BROWSER_PORT="$PORT_B" HORSE_BROWSER_PROFILE="$PROF_B" HORSE_SESSION="$SHARED" \
  "$HB" <<<"open_tab('file://$ROOT_DIR/s2.html')" >/dev/null 2>&1
urls() { curl -s -m 3 "http://127.0.0.1:$1/json/list" | tr ',' '\n' | grep -o '"url":[^,]*' | tr '\n' ' '; }
ua="$(urls "$PORT_A")"; ub="$(urls "$PORT_B")"
if [[ "$ua" == *s1.html* && "$ua" != *s2.html* && "$ub" == *s2.html* && "$ub" != *s1.html* ]]; then
  pass "one session driving a second browser lands in that browser (daemon re-pinned)"
else
  fail "one session driving a second browser lands in that browser (daemon re-pinned)" \
       "A=$(grep -o 's[12].html' <<<"$ua" | tr '\n' ' ') B=$(grep -o 's[12].html' <<<"$ub" | tr '\n' ' ')"
fi

# ── [6] a WRONG seed is refused, never trusted ───────────────────────────────
# The seed is a hint, not an authority: point instance A's extension at instance B and A's
# Monitor must sit blank rather than screencast B's tabs. Without the nonce proof this is the
# original bug with extra steps, and every other check here would still pass.
sw "$PORT_A" "chrome.storage.local.set({hbCdpPort:$PORT_B}).then(()=>1)" >/dev/null
wrote="$(sw "$PORT_A" "chrome.storage.local.get('hbCdpPort').then(o=>String(o.hbCdpPort))")"
monitor "$PORT_A" "setTimeout(()=>location.reload(),0)" >/dev/null 2>&1
lied=""
for _ in $(seq 1 12); do
  sleep 1
  lied="$(monitor "$PORT_A" "JSON.stringify({cdp:CDP,ws:ws?ws.readyState:null})")"
  [[ "$lied" == *"$PORT_B"* ]] && break
done
# The lie has to have LANDED and the Monitor has to still be answering, or "it didn't attach
# to B" would pass for the boring reasons (write failed / page dead) instead of the real one.
if [ "$wrote" = "$PORT_B" ] && [[ "$lied" == *'"cdp"'* ]] && [[ "$lied" != *"$PORT_B"* ]]; then
  pass "a seed pointing at another browser is refused (proof beats hint)"
else
  fail "a seed pointing at another browser is refused (proof beats hint)" "seed=$wrote A=$lied"
fi
sw "$PORT_A" "chrome.storage.local.set({hbCdpPort:$PORT_A}).then(()=>1)" >/dev/null

# ── [7] each browser NAMES itself on its toolbar button ──────────────────────
# Several identical windows, and the Monitor is a pinned tab — a pinned tab shows only its
# favicon, so the badge is the only place on screen that says which instance you are looking at.
badge_a="$(sw "$PORT_A" "chrome.action.getBadgeText({}).then(t=>String(t))")"
badge_b="$(sw "$PORT_B" "chrome.action.getBadgeText({}).then(t=>String(t))")"
if [ "$badge_a" = "$PORT_A" ] && [ "$badge_b" = "$PORT_B" ]; then
  pass "each toolbar badge shows its own instance's port"
else
  fail "each toolbar badge shows its own instance's port" "A=$badge_a B=$badge_b"
fi

# ── [8] source guard: the extension carries no hardcoded debug port ──────────
if grep -nE "127\.0\.0\.1:9[0-9]{3}\b" "$REPO"/extension/*.js >/dev/null 2>&1; then
  fail "extension has no hardcoded CDP port" "$(grep -nE "127\.0\.0\.1:9[0-9]{3}\b" "$REPO"/extension/*.js | head -3 | tr '\n' ' ')"
else
  pass "extension has no hardcoded CDP port"
fi

# ── [9] source guard: per-instance launcher state derives from the profile ───
# A global path here means two instances clobber each other: a concurrent relaunch reopens
# the wrong browser's tabs, and one instance's GPU heal corrupts the other's kill backoff.
bad="$(grep -nE '\$HOME/\.config/horse-browser/\.(relaunch-tabs\.json|last-reap)|HEAL_STAMP="\$HOME' "$HB" || true)"
if [ -z "$bad" ]; then
  pass "relaunch-tabs + heal + reap stamps are keyed to the profile, not global"
else
  fail "relaunch-tabs + heal + reap stamps are keyed to the profile, not global" "$(tr '\n' ' ' <<<"$bad")"
fi

say ""
say "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ] || { printf '  ✗ %s\n' "${FAILED[@]}"; exit 1; }
