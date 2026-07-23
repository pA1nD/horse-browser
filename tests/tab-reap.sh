#!/usr/bin/env bash
# tests/tab-reap.sh — tab-leak characterization + the orphan-tab reaper.
#
# Leaks that accumulate in the shared browser: a tab-less session's first CDP read makes
# _hb_home create an about:blank it never closes, and a process killed mid-open abandons its
# tab. Both leave tabs in a group whose agent SESSION has ended. reap_orphan_tabs closes tabs
# in any group that isn't a live session's — verified here to clean the dead, spare the live,
# and refuse to run when it can see no live session (the safety stop).
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HB="${HB:-$HERE/../bin/horse-browser}"
[ -x "$HB" ] || HB="$(command -v horse-browser || true)"
[ -x "$HB" ] || { echo "FATAL: horse-browser not found (set HB=…)"; exit 1; }
PORT="$(sed -n 's/^PORT=//p' "$HOME/.config/horse-browser/config" 2>/dev/null | head -1 | tr -d '"')"
PORT="${PORT:-9223}"
PASS=0; FAIL=0; SKIP=0; FAILED=()
say()  { printf '%s\n' "$*"; }
pass() { PASS=$((PASS+1)); say "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); FAILED+=("$1"); say "  ✗ $1${2:+ — $2}"; }
skip() { SKIP=$((SKIP+1)); say "  - $1 (skipped${2:+: $2})"; }

"$HB" >/dev/null 2>&1 || { say "FATAL: browser did not come up"; exit 1; }

# All this test's own CDP-reading helpers run under ONE disposable session so their _hb_home
# blanks land in a single group we tear down at the end — the test never litters.
HSESS="reaptest-$$-helper"
cleanup() {
  for p in $(pgrep -f browser_harness.daemon); do
    ps eww -o command= -p "$p" 2>/dev/null | tr ' ' '\n' | grep -qE "^HORSE_SESSION=reaptest-$$" && kill "$p" 2>/dev/null
  done
  sleep 1                 # let the kills propagate so the reaper sees these sessions as dead
  reap >/dev/null 2>&1 || true
}
trap cleanup EXIT

blanks() { curl -s "http://127.0.0.1:$PORT/json/list" \
  | python3 -c "import json,sys; print(sum(1 for t in json.load(sys.stdin) if t.get('type')=='page' and (t.get('url') or '') in ('','about:blank')))"; }
group_tabs() {  # count grouped page tabs whose group title contains $1 (a session's last-4, uppercased)
  HORSE_SESSION="$HSESS" "$HB" <<PY 2>/dev/null | sed -n 's/^GT //p'
sw = next(t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t.get("type")=="service_worker" and t.get("url","").startswith("chrome-extension://"))
s = cdp("Target.attachToTarget", targetId=sw, flatten=True)["sessionId"]
expr = "(async()=>{const g=await chrome.tabGroups.query({});const gid=new Set(g.filter(x=>x.title.includes('$1')).map(x=>x.id));const t=await chrome.tabs.query({});return t.filter(x=>gid.has(x.groupId)).length;})()"
print("GT", cdp("Runtime.evaluate", session_id=s, expression=expr, awaitPromise=True, returnByValue=True).get("result",{}).get("value"))
PY
}
daemon_pid_for() {  # echo the daemon pid whose HORSE_SESSION == $1
  for p in $(pgrep -f browser_harness.daemon); do
    ps eww -o command= -p "$p" 2>/dev/null | tr ' ' '\n' | grep -qx "HORSE_SESSION=$1" && { echo "$p"; return; }
  done
}
ung_blanks() {  # count UNGROUPED, non-pinned about:blank tabs (the stray-blank pile)
  HORSE_SESSION="$HSESS" "$HB" <<PY 2>/dev/null | sed -n 's/^UB //p'
sw = next(t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t.get("type")=="service_worker" and t.get("url","").startswith("chrome-extension://"))
s = cdp("Target.attachToTarget", targetId=sw, flatten=True)["sessionId"]
expr = "(async()=>{const t=await chrome.tabs.query({});return t.filter(x=>x.groupId<0 && !x.pinned && ((x.url||'')===''||x.url==='about:blank')).length;})()"
print("UB", cdp("Runtime.evaluate", session_id=s, expression=expr, awaitPromise=True, returnByValue=True).get("result",{}).get("value"))
PY
}
make_ungrouped_blank() {  # mint a raw ungrouped about:blank (like the daemon's attach-blank)
  curl -s -X PUT "http://127.0.0.1:$PORT/json/new?about:blank" >/dev/null 2>&1
}
reap() { HORSE_BROWSER_REAP_INTERVAL=0 HORSE_BROWSER_REAP_STAMP="$(mktemp)" "$HB" >/dev/null 2>&1; }

say "horse-browser tab-reap — port $PORT"

# reapDeadTabs must be loaded in the extension (survives a relaunch; MV3 SW may serve stale
# bytecode until a cache wipe) — otherwise the reaping checks can't run.
have_fn="$(HORSE_SESSION="$HSESS" "$HB" <<'PY' 2>/dev/null | sed -n 's/^FN //p'
sw = next((t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t.get("type")=="service_worker" and t.get("url","").startswith("chrome-extension://")), None)
if sw:
    s = cdp("Target.attachToTarget", targetId=sw, flatten=True)["sessionId"]
    print("FN", cdp("Runtime.evaluate", session_id=s, expression="typeof self.reapDeadTabs", returnByValue=True).get("result",{}).get("value"))
PY
)"

# ═════ 1. leak characterization ══════════════════════════════════════════════
say "[1] leak characterization"
b0="$(blanks)"
# measure the leak in isolation — no reaping mid-measurement (it would clean other orphans
# and skew the delta); this session touches CDP without opening a tab, so _hb_home leaks a
# blank it never closes (the exact count depends on the daemon's first-page attach, so we
# assert "leaks at least one", which is the characterization that matters).
HORSE_SESSION="reaptest-$$-bare" HORSE_BROWSER_NO_REAP=1 HORSE_BROWSER_NO_TAB_REAP=1 \
  "$HB" <<<'page_info()' >/dev/null 2>&1
b1="$(blanks)"
[ "$b1" -gt "$b0" ] \
  && pass "a tab-less read session leaks a never-closed about:blank ($b0->$b1)" \
  || fail "a tab-less read session leaks a blank" "$b0->$b1"

# Adoption fix: the daemon's ungrouped attach-blank must be ADOPTED into the session group,
# not left as a stray ungrouped orphan — so a read session grows GROUPED blanks, never the
# ungrouped pile. Assert the ungrouped count doesn't rise across a fresh read session.
u0="$(ung_blanks)"
HORSE_SESSION="reaptest-$$-adopt" HORSE_BROWSER_NO_REAP=1 HORSE_BROWSER_NO_TAB_REAP=1 \
  "$HB" <<<'page_info()' >/dev/null 2>&1
u1="$(ung_blanks)"
[ "${u1:-0}" -le "${u0:-0}" ] \
  && pass "the daemon's ungrouped blank is adopted, not leaked (ungrouped $u0->$u1)" \
  || fail "daemon blank adopted, not leaked as an ungrouped stray" "ungrouped $u0->$u1"

# ═════ 2. reaper: dead vs live ═══════════════════════════════════════════════
say "[2] orphan-tab reaper"
if [ "$have_fn" != "function" ]; then
  skip "reaper closes an ended session's tabs" "extension reapDeadTabs not loaded (relaunch+SW-wipe)"
  skip "reaper spares a live session's tabs" "extension reapDeadTabs not loaded"
else
  # DEAD session: open a tab, then kill its daemon so the session reads as ended.
  DEAD="reaptest-$$-dead"; DTAIL="$(printf %s "$DEAD" | tail -c 4 | tr a-z A-Z)"
  HORSE_SESSION="$DEAD" "$HB" <<'PY' >/dev/null 2>&1
import urllib.parse
bh_open("data:text/html,"+urllib.parse.quote("<title>DEAD-WORK</title>x"))   # left open on purpose
wait_for_load()
PY
  dpid="$(daemon_pid_for "$DEAD")"
  [ -n "$dpid" ] && kill "$dpid" 2>/dev/null; sleep 1     # session now has no daemon -> ended
  before_dead="$(group_tabs "$DTAIL")"

  # LIVE session: open a tab AND keep a daemon alive for it for the duration.
  LIVE="reaptest-$$-live"; LTAIL="$(printf %s "$LIVE" | tail -c 4 | tr a-z A-Z)"
  HORSE_SESSION="$LIVE" "$HB" <<'PY' >/dev/null 2>&1
import urllib.parse
bh_open("data:text/html,"+urllib.parse.quote("<title>LIVE-WORK</title>x"))
wait_for_load()
PY
  before_live="$(group_tabs "$LTAIL")"

  reap    # triggers reap_orphan_daemons + reap_orphan_tabs; LIVE's daemon keeps it alive
  after_dead="$(group_tabs "$DTAIL")"
  after_live="$(group_tabs "$LTAIL")"

  { [ "${before_dead:-0}" -ge 1 ] && [ "${after_dead:-0}" -eq 0 ]; } \
    && pass "reaper closed the ended session's tab(s) ($before_dead->$after_dead)" \
    || fail "reaper closes an ended session's tabs" "dead group $before_dead->$after_dead"
  { [ "${before_live:-0}" -ge 1 ] && [ "${after_live:-0}" -eq "${before_live:-0}" ]; } \
    && pass "reaper spared the live session's tab(s) ($after_live kept)" \
    || fail "reaper spares a live session's tabs" "live group $before_live->$after_live"

  # cleanup the live session's leftover daemon + tab
  lpid="$(daemon_pid_for "$LIVE")"; [ -n "$lpid" ] && kill "$lpid" 2>/dev/null
  reap >/dev/null 2>&1

  # a stray UNGROUPED about:blank (a daemon attach-blank that escaped adoption) must be reaped
  ub0="$(ung_blanks)"
  make_ungrouped_blank; sleep 0.5
  ub1="$(ung_blanks)"
  reap
  ub2="$(ung_blanks)"
  { [ "${ub1:-0}" -gt "${ub0:-0}" ] && [ "${ub2:-0}" -le "${ub0:-0}" ]; } \
    && pass "reaper closes a stray ungrouped about:blank ($ub0->$ub1->$ub2)" \
    || fail "reaper closes a stray ungrouped about:blank" "$ub0->$ub1->$ub2"
fi

# ═════ 3. safety: empty live set never reaps ═════════════════════════════════
say "[3] safety stop"
if [ "$have_fn" != "function" ]; then
  skip "empty live set is refused" "extension reapDeadTabs not loaded"
else
  out="$(HORSE_SESSION="$HSESS" "$HB" <<'PY' 2>/dev/null | sed -n 's/^SAFE //p'
sw = next(t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t.get("type")=="service_worker" and t.get("url","").startswith("chrome-extension://"))
s = cdp("Target.attachToTarget", targetId=sw, flatten=True)["sessionId"]
r = cdp("Runtime.evaluate", session_id=s, expression="self.reapDeadTabs([])", awaitPromise=True, returnByValue=True)
import json
print("SAFE", json.dumps(r.get("result",{}).get("value")))
PY
)"
  grep -q "no-live-sessions" <<<"$out" && pass "reapDeadTabs([]) refuses to act (no-live-sessions)" \
    || fail "empty live set is refused" "$out"
fi

say ""
say "── $PASS passed, $FAIL failed, $SKIP skipped"
[ "$FAIL" -gt 0 ] && { for f in "${FAILED[@]}"; do say "   FAILED: $f"; done; exit 1; }
exit 0
