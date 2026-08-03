#!/usr/bin/env bash
# tests/challenge-frame.sh — reading a control INSIDE a cross-origin challenge frame.
#
# The claim under test: a challenge sealed in a cross-origin iframe is NOT out of reach. The
# frame is its own CDP target, so we can attach, read the control's real rect, and convert it
# to page coordinates — instead of escalating to vision or guessing a fraction of the frame
# (the PerimeterX path hardcoded 0.486×w, 0.553×h, which is right until a layout changes).
#
# Ground truth is the whole point. A live captcha cannot tell you where it put its button, and
# it moves under you — so this serves its own challenge on a DIFFERENT SITE with the control at
# coordinates the fixture chose, and asserts the solver returns those coordinates.
#
# Positions are RANDOM per run. A solver that hardcodes anything — the frame offset, a centre
# fraction, a vendor's usual layout — passes once and then fails, which is the point.
#
# Real out-of-process frames: site isolation keys on SITE, so two ports on 127.0.0.1 are
# cross-origin but SAME-site and the frame stays in the parent's process, with no separate
# target to attach to. --host-resolver-rules maps distinct hostnames, which produces the real
# thing (verified: the frame appears as its own iframe target).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
PY="$ROOT/harness/.venv/bin/python"
PASS=0; FAIL=0; FAILED=()
pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); FAILED+=("$1"); echo "  ✗ $1${2:+ — $2}"; }
port() { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'; }

echo "horse-browser challenge-frame — reading a sealed cross-origin control"
"$ROOT/bin/horse-browser" harness-setup >/dev/null 2>&1
[ -x "$PY" ] || { echo "FATAL: harness venv missing"; exit 1; }
BIN="$(sed -n 's/^BROWSER_BIN=//p' "$HOME/.config/horse-browser/config" 2>/dev/null | tr -d '"')"
[ -x "$BIN" ] || { echo "FATAL: no Chrome (horse-browser update)"; exit 1; }

W="$(mktemp -d -t hb-cfr.XXXXXX)"
CPORT="$(port)"
cleanup() {
  for p in ${FIXPIDS:-}; do kill "$p" 2>/dev/null; done
  pids="$(ps -ww -o pid= -o command= -ax 2>/dev/null | grep -F -- "--user-data-dir=$W/p" | grep -v grep | awk '{print $1}')"
  for p in $pids; do kill "$p" 2>/dev/null; done
  sleep 1; for p in $pids; do kill -9 "$p" 2>/dev/null; done
  rm -rf "$W"
}
trap cleanup EXIT
FIXPIDS=""

# One browser for the whole suite, launched focus-free (a test must not take the operator's
# keyboard — see tests/attached-mode.sh, which learned this the hard way).
CHROME_ARGS=( --remote-debugging-port="$CPORT" --user-data-dir="$W/p"
              --no-first-run --no-default-browser-check
              --host-resolver-rules="MAP parent.test 127.0.0.1, MAP frame.test 127.0.0.1"
              about:blank )
APP="${BIN%/Contents/MacOS/*}"
if [ "$(uname -s)" = "Darwin" ] && [ "$APP" != "$BIN" ] && [ -d "$APP" ]; then
  open -g -n -a "$APP" --args "${CHROME_ARGS[@]}"
else
  "$BIN" "${CHROME_ARGS[@]}" >/dev/null 2>&1 &
fi
for _ in $(seq 1 60); do curl -sf -m 1 "http://127.0.0.1:$CPORT/json/version" >/dev/null 2>&1 && break; sleep 0.5; done
curl -sf -m 2 "http://127.0.0.1:$CPORT/json/version" >/dev/null 2>&1 \
  || { echo "FATAL: browser did not come up"; exit 1; }

hb() { BU_CDP_URL="http://127.0.0.1:$CPORT" BU_NAME="hb-cfr$$" HORSE_SESSION="cfr-$$" \
       PYTHONPATH="$ROOT/harness" "$PY" -m horse_harness.run <<<"$1" 2>&1; }

# --- one case: random control position, assert the reported page coords ------------------
one() {   # one <kind> <label>
  local kind="$1" label="$2"
  local cx cy cw ch p1 p2 fx
  cx=$(( (RANDOM % 300) + 20 ))          # inside the 400x300 frame, clear of the decoys
  cy=$(( (RANDOM % 140) + 90 ))
  cw=$(( (RANDOM % 60) + 24 ))
  ch=$(( (RANDOM % 30) + 20 ))
  p1="$(port)"; p2="$(port)"
  python3 "$HERE/lib/oopif-fixture.py" "$p1" "$p2" "$kind" "$cx" "$cy" "$cw" "$ch" > "$W/fx.json" &
  FIXPIDS="$FIXPIDS $!"
  for _ in $(seq 1 30); do [ -s "$W/fx.json" ] && break; sleep 0.2; done
  local ex ey
  ex=$(python3 -c "import json;print(json.load(open('$W/fx.json'))['expect_x'])" 2>/dev/null)
  ey=$(python3 -c "import json;print(json.load(open('$W/fx.json'))['expect_y'])" 2>/dev/null)
  [ -n "$ex" ] || { fail "$label" "fixture did not start"; return; }

  local out
  out="$(hb "
import time
from horse_harness.helpers import _xorigin_challenge, _frame_control
goto_url('http://parent.test:$p1/'); wait_for_load(); time.sleep(1.2)
xo = _xorigin_challenge()
fc = _frame_control(xo)
print('XO', xo)
# One value per line, already parsed. Regexing a Python dict repr out of shell is how the
# first version of this test 'failed' while the solver was returning correct coordinates.
if fc:
    print('FCX', fc['x']); print('FCY', fc['y']); print('FCK', fc['kind'])
")"
  local gx gy gk
  gx="$(sed -n 's/^FCX //p' <<<"$out")"
  gy="$(sed -n 's/^FCY //p' <<<"$out")"
  gk="$(sed -n 's/^FCK //p' <<<"$out")"
  if [ -z "$gx" ]; then
    fail "$label" "no control found: $(grep -E '^(XO|FC)' <<<"$out" | tr '\n' ' ' | cut -c1-120)"
    return
  fi
  # ±3px: the fixture's expectation uses integer division on the centre, and the browser
  # rounds subpixel layout. Anything larger means the offset maths is actually wrong.
  local dx=$(( gx > ex ? gx - ex : ex - gx )) dy=$(( gy > ey ? gy - ey : ey - gy ))
  if [ "$dx" -le 3 ] && [ "$dy" -le 3 ]; then
    pass "$label — found ($gx,$gy), expected ($ex,$ey), kind=$gk"
  else
    fail "$label" "got ($gx,$gy) expected ($ex,$ey) — off by ($dx,$dy)"
  fi
  [ "$gk" = "$kind" ] || fail "$label kind" "reported '$gk', fixture placed '$kind'"
}

echo "[1] each control kind, at a random position"
for k in checkbox press-hold slider button; do one "$k" "$k"; done

echo "[2] repeated with fresh random positions (a hardcoded answer cannot survive this)"
for i in 1 2 3; do one checkbox "checkbox run $i"; done

echo
if [ "$FAIL" -eq 0 ]; then echo "── $PASS passed, 0 failed"; else
  echo "── $PASS passed, $FAIL failed"; for f in "${FAILED[@]}"; do echo "   ✗ $f"; done; fi
exit $((FAIL > 0))
