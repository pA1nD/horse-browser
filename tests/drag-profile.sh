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
window.LOG=[]; window.PTR=[];
var t0=performance.now();
// POINTER events, not the compat mouse ones. Chrome rounds mousemove coordinates to whole
// pixels, which destroys the sub-pixel detail and manufactures repeats — in a recorded human
// session, the same gesture read 100% fractional with zero repeats as pointermove, and 0%
// fractional with 18.4% repeats as mousemove. Measuring the lossy copy meant comparing our
// gesture against an artefact of Chrome's rounding rather than against the hand.
['pointermove','pointerdown','pointerup'].forEach(function(n){
  document.addEventListener(n,function(e){
    // Full precision. Rounding to 0.1px here MANUFACTURED the repeated positions this file
    // then measured, and the human baseline it compared them against came from a recorder that
    // rounded the same way — so both sides agreed on an artefact. A real pointer's coordinates
    // are sub-pixel: in a recorded human session, 256 of 256 samples were fractional and not one
    // repeated the previous position exactly.
    LOG.push([n==='pointermove'?'m':(n==='pointerdown'?'d':'u'),
              e.clientX, e.clientY, Math.round(performance.now()-t0)]);
  },true);
});
// What the pointer says about ITSELF, not just where it was. A physical pointer cannot report
// a button held with no force behind it, and reading that takes one property access.
['pointerdown','pointermove','pointerup'].forEach(function(n){
  document.addEventListener(n,function(e){
    if (PTR.length < 400) PTR.push({t:n, p:e.pressure, b:e.buttons, k:e.pointerType,
                                    w:e.width, h:e.height, prim:e.isPrimary});
  },true);
});
</script>
HTML
# Serve the fixture, and PROVE it is serving before anything depends on it. port() binds an
# ephemeral port, closes it, and hands back the number — so between that and http.server's own
# bind, the OS is free to hand the same port to any outgoing connection. On a busy machine
# (a browser per agent, a CDP websocket per session) that happens, http.server dies on
# "Address already in use", Chrome gets chrome-error://chromewebdata, and every gesture is
# recorded against a blank page. The test then failed as `StopIteration` deep in the stats —
# reading as "the drag produced no pointerup" when the truth was "there was no page".
for attempt in 1 2 3; do
  ( cd "$W" && python3 -m http.server "$SPORT" >/dev/null 2>&1 ) & SRVPID=$!
  for _ in $(seq 1 40); do
    curl -sf -m 1 "http://127.0.0.1:$SPORT/page.html" >/dev/null 2>&1 && break
    sleep 0.25
  done
  curl -sf -m 2 "http://127.0.0.1:$SPORT/page.html" >/dev/null 2>&1 && break
  kill "$SRVPID" 2>/dev/null; SPORT="$(port)"   # lost the race — take a fresh port and retry
done
curl -sf -m 2 "http://127.0.0.1:$SPORT/page.html" >/dev/null 2>&1 \
  || { echo "FATAL: fixture server never came up (last port $SPORT)"; exit 1; }

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

# The measurement runs from a FILE, not a heredoc inside $( ). Bash 3.2 — still what macOS
# ships — mis-parses that combination once the heredoc body gets complex, and reports it as
# "unexpected EOF while looking for matching `)'" pointing at the line the substitution
# starts on, which is nowhere near the text it actually choked on.
cat > "$W/measure.py" <<'PYEOF'
import json, os, statistics

# open_tab, not goto_url: the about:blank window Chrome creates at launch never acknowledges
# Input.dispatchMouseEvent on macOS when the browser was started in the background, so every
# gesture call sits until the IPC timeout and the run reads as a hang. A tab this session opens
# for itself acks in ~10ms. (Agent code always takes this path; only ad-hoc CDP hits the other.)
open_tab("http://127.0.0.1:%s/page.html" % os.environ["SPORT"]); wait_for_load()
runs = []
for _ in range(12):
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
res["frac_pct"] = round(100.0 * sum(sum(1 for e in moves(r) if e[1] != int(e[1]) or e[2] != int(e[2]))
                                    for r in runs) / sum(len(moves(r)) for r in runs))
res["dupe_pct"] = round(100.0 * sum(sum(1 for a, b in zip(moves(r), moves(r)[1:]) if a[1:3] == b[1:3])
                                    for r in runs) / sum(len(moves(r)) for r in runs), 1)
# 8. the pointer's self-description. CDP defaults `force` to 0, so every gesture this tool sent
#    reported a button down with zero pressure — a state no physical pointer produces.
ptr = json.loads(js("JSON.stringify(window.PTR)"))
held = [e for e in ptr if e["b"] == 1]
free = [e for e in ptr if e["b"] == 0]
res["pressure_held"] = sorted({e["p"] for e in held}) if held else []
res["pressure_free"] = sorted({e["p"] for e in free}) if free else []
res["ptr_shape"] = sorted({(e["k"], e["w"], e["h"], e["prim"]) for e in ptr})
print(json.dumps(res))
PYEOF

OUT="$(BU_CDP_URL="http://127.0.0.1:$CPORT" BU_NAME="hb-drag$$" HORSE_SESSION="drag-$$" \
      HORSE_BROWSER_IPC_TIMEOUT=30 \
      PYTHONPATH="$ROOT/harness" SPORT="$SPORT" "$PY" -m horse_harness.run < "$W/measure.py")"
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
# Bounds taken from the recorded drags themselves, not picked: the 14 human traces in
# tests/lib/traces run 1.08s .. 3.42s, median 1.75s. The old ceiling of 3.0 was tighter than the
# hands it claims to encode — the slowest recorded HUMAN would have failed it — while the floor
# of 0.45 was half the fastest one. On an idle machine we sit at 1.5-2.1s; the upper half of the
# band only comes into play when dispatch is so expensive that drag() stops trading samples for
# time and runs long on purpose, and a 3s drag is a thing a hand demonstrably does.
chk "the drag takes a hand's time, not a loop's"         "all(1.0 <= d <= 3.5 for d in v)" dur
# Bimodal in the recorded hand: 7 of 14 drags overshot 5-24px, the other 7 not at all. A drag
# that always overshoots by a little is the average of two behaviours and resembles neither.
#
# This used to count how many of the 12 overshot and require 2..10 — which is a fair coin asked
# to land between 2 and 10 heads in 12 throws, and fails 0.6% of the time on perfectly correct
# code (Monte-Carlo'd over 400 simulated runs; also seen for real). A failure rate that low is
# worse than a higher one: it always arrives as "just re-run it", so eventually nobody believes
# the check at all.
#
# The frequency claim moved to harness/tests/unit/test_drag_shape.py, where a virtual clock buys
# 400 samples for less than a second and a wrong rate is unmissable. What is left here is the
# part that needs a real browser and cannot flake: the overshoots that DO occur are real ones,
# never a small constant — the recorded hands leave a gap between ~0 and 5px, and so must we.
chk "overshoot is a real one or none, never a nudge"    "all(o < 1.5 or o >= 4.0 for o in v)" overshoot
chk "samples are not metronomic"                         "min(v) >= 0.12"            dt_cv
# Judged on the DISTRIBUTION, not run by run. This is a shape property measured through
# randomised sleeps and a browser under load — with the whole suite running, individual drags
# scatter to 0.51 while the set still centres near 0.39, and symmetric easing (the tell being
# tested for) sits at 0.50 every time. A per-run threshold turns that scatter into a flaky
# failure and teaches nothing; the median moves only if the easing actually changes. Same
# reasoning as harness/tests/unit/test_slider.py, which scores a matcher the same way.
chk "it accelerates faster than it decelerates"          "sorted(v)[len(v)//2] <= 0.45" half_at
chk "no run decelerates first"                           "max(v) <= 0.62"            half_at
chk "it releases on the target"                          "all(e <= 2.0 for e in v)"  err_x
chk "it holds briefly before letting go"                 "all(h >= 40 for h in v)"   hold
# Bounds from the full-precision human recording (tests/lib/traces/events-human.json): 256 of
# 256 pointer samples fractional, ZERO exact repeats, longest run 1. The earlier, looser numbers
# here (runs of 3, repeats at 8.5%) were an artefact of a recorder that rounded to 0.1px — once
# both sides keep full precision, a hand simply never reports the same position twice.
chk "the pointer never stalls in place"                  "v <= 2"                    max_run
chk "positions do not repeat, as a real pointer's do not" "v <= 2.0"                 dupe_pct
# The tell this replaced: every coordinate a whole number. Chrome passes fractional dispatched
# coordinates through untouched, and a real pointer is sub-pixel on any display with a device
# pixel ratio above 1 — 256 of 256 samples in the human recording. All-integer positions across
# a gesture is one line for a page to check, and this code used to round every one of them.
chk "positions are sub-pixel, as a real pointer's are"   "v >= 80"                   frac_pct
chk "a held button reports real pressure"                "v == [0.5]"                pressure_held
chk "a free button reports no pressure"                  "v == [0]"                  pressure_free
chk "it describes itself as one primary mouse"           "v == [['mouse', 1, 1, True]]" ptr_shape

echo
if [ "$FAIL" -eq 0 ]; then echo "── $PASS passed, 0 failed"; else
  echo "── $PASS passed, $FAIL failed"; for f in "${FAILED[@]}"; do echo "   ✗ $f"; done; fi
exit $((FAIL > 0))
