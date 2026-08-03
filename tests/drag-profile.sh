#!/usr/bin/env bash
# tests/drag-profile.sh — the SHAPE of a drag, measured from the events the page receives.
#
# Landing the right coordinates is not enough for a slide-to-verify widget: it scores the whole
# trace. Measured on a live DataDome slider, a drag that started and ended exactly right still
# failed, repeatedly, and the failures cost enough reputation that the address ended up hard
# blocked. So the motion profile is the thing under test, and the page's own event log is the
# only honest witness — asserting on our own intent would just be asking the code to agree
# with itself.
#
# Every assertion here is a property a HAND has and a naive loop does not. None of them pin a
# constant the implementation chose; they are all bands wide enough for the randomisation to
# move inside and narrow enough that removing the behaviour fails them.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
PY="$ROOT/harness/.venv/bin/python"
PASS=0; FAIL=0; FAILED=()
pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); FAILED+=("$1"); echo "  ✗ $1${2:+ — $2}"; }
port() { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'; }

echo "horse-browser drag-profile — is the motion shaped like a hand?"
"$ROOT/bin/horse-browser" harness-setup >/dev/null 2>&1
BIN="$(sed -n 's/^BROWSER_BIN=//p' "$HOME/.config/horse-browser/config" 2>/dev/null | tr -d '"')"
[ -x "$BIN" ] || { echo "FATAL: no Chrome (horse-browser update)"; exit 1; }

W="$(mktemp -d -t hb-drag.XXXXXX)"; CPORT="$(port)"; SPORT="$(port)"
cleanup() {
  kill ${SRVPID:-0} 2>/dev/null
  pids="$(ps -ww -o pid= -o command= -ax 2>/dev/null | grep -F -- "--user-data-dir=$W/p" | grep -v grep | awk '{print $1}')"
  for p in $pids; do kill "$p" 2>/dev/null; done
  sleep 1; for p in $pids; do kill -9 "$p" 2>/dev/null; done
  rm -rf "$W"
}
trap cleanup EXIT

cat > "$W/page.html" <<'HTML'
<!doctype html><meta charset=utf-8><title>drag recorder</title>
<body style="margin:0">
<div id="h" style="position:absolute;left:120px;top:200px;width:60px;height:40px;
     background:#4a90d9;cursor:grab"></div>
<script>
window.LOG=[];
var t0=performance.now();
['mousemove','mousedown','mouseup'].forEach(function(n){
  document.addEventListener(n,function(e){
    LOG.push([n[5]==='m'?'m':(n==='mousedown'?'d':'u'),
              Math.round(e.clientX*10)/10, Math.round(e.clientY*10)/10,
              Math.round(performance.now()-t0)]);
  },true);
});
</script>
HTML
( cd "$W" && python3 -m http.server "$SPORT" >/dev/null 2>&1 ) & SRVPID=$!

CHROME_ARGS=( --remote-debugging-port="$CPORT" --user-data-dir="$W/p"
              --no-first-run --no-default-browser-check
              # Without these a backgrounded test browser never acks Input.dispatchMouseEvent:
              # the renderer is throttled, every gesture call sits until the IPC timeout, and
              # the run looks like a hang rather than a missing flag. The real launcher passes
              # them for the same reason (bin/horse-browser).
              --disable-renderer-backgrounding
              --disable-background-timer-throttling
              --disable-backgrounding-occluded-windows about:blank )
APP="${BIN%/Contents/MacOS/*}"
if [ "$(uname -s)" = "Darwin" ] && [ "$APP" != "$BIN" ] && [ -d "$APP" ]; then
  open -g -n -a "$APP" --args "${CHROME_ARGS[@]}"
else
  "$BIN" "${CHROME_ARGS[@]}" >/dev/null 2>&1 &
fi
for _ in $(seq 1 60); do curl -sf -m 1 "http://127.0.0.1:$CPORT/json/version" >/dev/null 2>&1 && break; sleep 0.5; done
curl -sf -m 2 "http://127.0.0.1:$CPORT/json/version" >/dev/null 2>&1 || { echo "FATAL: chrome did not come up"; exit 1; }

OUT="$(BU_CDP_URL="http://127.0.0.1:$CPORT" BU_NAME="hb-drag$$" HORSE_SESSION="drag-$$" \
      HORSE_BROWSER_IPC_TIMEOUT=30 \
      PYTHONPATH="$ROOT/harness" SPORT="$SPORT" "$PY" -m horse_harness.run <<'PYEOF'
import json, os, statistics

# open_tab, not goto_url: the about:blank window Chrome creates at launch never acknowledges
# Input.dispatchMouseEvent on macOS when the browser was started in the background, so every
# gesture call sits until the IPC timeout and the run reads as a hang. A tab this session opens
# for itself acks in ~10ms. (Agent code always takes this path; only ad-hoc CDP hits the other.)
open_tab("http://127.0.0.1:%s/page.html" % os.environ["SPORT"]); wait_for_load()
runs = []
for _ in range(6):
    js("window.LOG=[]")
    drag((150, 220), to=(430, 220))
    runs.append(json.loads(js("JSON.stringify(window.LOG)")))

res = {}
def moves(r):   return [e for e in r if e[0] == "m"]
def press(r):   return next(e for e in r if e[0] == "d")
def rel(r):     return next(e for e in r if e[0] == "u")

# 1. approach: movement BEFORE the button goes down
res["approach"] = min(len([e for e in moves(r) if e[3] < press(r)[3]]) for r in runs)
# 2. duration press->release, in seconds
res["dur"] = [round((rel(r)[3] - press(r)[3]) / 1000.0, 2) for r in runs]
# 3. overshoot: furthest x during the drag vs where it was released
res["overshoot"] = [round(max(e[1] for e in moves(r)) - rel(r)[1], 1) for r in runs]
# 4. sample spacing must not be metronomic
sp = []
for r in runs:
    ts = [e[3] for e in moves(r)]
    d = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    sp.append(round(statistics.pstdev(d) / (statistics.mean(d) or 1), 3))
res["dt_cv"] = sp
# 5. asymmetry: what fraction of the elapsed time is spent covering the FIRST HALF of the
#    distance. A hand accelerates briskly and settles slowly, so it is well under 0.5;
#    symmetric easing — the tell this replaced — sits at exactly 0.5.
#    Measured over the whole first half rather than at the single fastest sample: with
#    randomised inter-sample sleeps, one short sleep makes any step look like the peak, so
#    "where was the maximum" reports the sleep jitter, not the easing.
half = []
for r in runs:
    m = [e for e in moves(r) if e[3] >= press(r)[3]]
    x0, x1, t0 = m[0][1], rel(r)[1], m[0][3]
    span = (m[-1][3] - t0) or 1
    mid = next((e for e in m if abs(e[1] - x0) >= abs(x1 - x0) / 2.0), m[-1])
    half.append(round((mid[3] - t0) / span, 2))
res["half_at"] = half
# 6. accuracy at release, and the pause before it
res["err_x"] = [round(abs(rel(r)[1] - 430), 1) for r in runs]
res["hold"]  = [rel(r)[3] - max(e[3] for e in moves(r)) for r in runs]
# 7. no STALL: a hand can put two samples on the same tenth of a pixel while it hesitates, so a
#    single repeat proves nothing. A loop that stops emitting new positions shows up as a run of
#    them, and as a repeat rate no hand reaches.
def runlen(r):
    best = cur = 1
    for a, b in zip(moves(r), moves(r)[1:]):
        cur = cur + 1 if a[1:3] == b[1:3] else 1
        best = max(best, cur)
    return best
res["max_run"] = max(runlen(r) for r in runs)
res["dupe_pct"] = round(100.0 * sum(sum(1 for a, b in zip(moves(r), moves(r)[1:]) if a[1:3] == b[1:3])
                                    for r in runs) / sum(len(moves(r)) for r in runs), 1)
print(json.dumps(res))
PYEOF
)"
echo "$OUT" | tail -1 > "$W/res.json"
q() { python3 -c "import json,sys;print(json.load(open('$W/res.json'))['$1'])" 2>/dev/null; }

python3 -c "import json;json.load(open('$W/res.json'))" 2>/dev/null || {
  echo "  ✗ measurement failed: $(echo "$OUT" | tail -3)"; exit 1; }

chk() { # chk <label> <python-expr over v> <field>
  local label="$1" expr="$2" fld="$3"
  if python3 -c "
import json,sys
v=json.load(open('$W/res.json'))['$fld']
sys.exit(0 if ($expr) else 1)"; then pass "$label ($fld=$(q "$fld"))"; else fail "$label" "$fld=$(q "$fld")"; fi
}

chk "the pointer approaches the handle before pressing"  "v >= 4"                    approach
chk "the drag takes a hand's time, not a loop's"         "all(0.45 <= d <= 3.0 for d in v)" dur
chk "it overshoots, then corrects back onto the target"  "sum(1 for o in v if o >= 1.5) >= 4" overshoot
chk "samples are not metronomic"                         "min(v) >= 0.12"            dt_cv
chk "it accelerates faster than it decelerates"          "sum(1 for p in v if p <= 0.47) >= 5" half_at
chk "it releases on the target"                          "all(e <= 2.0 for e in v)"  err_x
chk "it holds briefly before letting go"                 "all(h >= 40 for h in v)"   hold
chk "the pointer never stalls in place"                  "v <= 2"                    max_run
chk "repeats stay at hand-rate, not loop-rate"           "v < 5.0"                   dupe_pct

echo
if [ "$FAIL" -eq 0 ]; then echo "── $PASS passed, 0 failed"; else
  echo "── $PASS passed, $FAIL failed"; for f in "${FAILED[@]}"; do echo "   ✗ $f"; done; fi
exit $((FAIL > 0))
