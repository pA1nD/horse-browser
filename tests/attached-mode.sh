#!/usr/bin/env bash
# tests/attached-mode.sh — drive a browser we did NOT launch.
#
# Every other suite runs against a horse-browser-launched Chrome: our extension loaded, our
# launch flags, our profile. That means all of it passes even if the code silently depends on
# the extension being there. This one launches a BARE Chrome for Testing —
# --remote-debugging-port and --user-data-dir, nothing else, NO --load-extension — and points
# the harness straight at it.
#
# It is the only test that proves the three things the registry rewrite was for:
#   • which tabs are a session's is answered without any extension (the registry is the truth)
#   • the realness mask still lands, applied per session over CDP by the daemon
#   • nothing in the normal path crashes or hangs when the service worker simply isn't there
#
# Deliberately does NOT use bin/horse-browser to start the browser — that would defeat the
# point. It talks to horse_harness.run the way the launcher does, with BU_CDP_URL pointed
# somewhere the launcher has never been.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
PY="$ROOT/harness/.venv/bin/python"
PASS=0; FAIL=0; SKIP=0
say()  { printf '%s\n' "$*"; }
pass() { PASS=$((PASS+1)); say "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); say "  ✗ $1${2:+ — $2}"; }
skip() { SKIP=$((SKIP+1)); say "  - $1 (skipped${2:+: $2})"; }

"$ROOT/bin/horse-browser" harness-setup >/dev/null 2>&1
[ -x "$PY" ] || { say "FATAL: harness venv missing"; exit 1; }

BIN="$(sed -n 's/^BROWSER_BIN=//p' "$HOME/.config/horse-browser/config" 2>/dev/null | tr -d '"')"
[ -x "$BIN" ] || { say "FATAL: no Chrome for Testing binary (run: horse-browser update)"; exit 1; }

PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
WORK="$(mktemp -d -t hb-attached.XXXXXX)"
SESS="attached-$$-$RANDOM"
BU="hb-${SESS##*-}"
REG="$HOME/.config/horse-browser/tabs/$BU"

cleanup() {
  for p in $(pgrep -f "(browser|horse)_harness.daemon" 2>/dev/null); do
    ps eww -o command= -p "$p" 2>/dev/null | tr ' ' '\n' | grep -qx "HORSE_SESSION=$SESS" && kill "$p" 2>/dev/null
  done
  [ -n "${CHROME_PID:-}" ] && kill "$CHROME_PID" 2>/dev/null
  [ -n "${REFL_PID:-}"  ] && { kill "$REFL_PID"; wait "$REFL_PID"; } 2>/dev/null
  rm -f "$REG"
  rm -rf "$WORK"
}
trap cleanup EXIT

# ── a BARE browser: the two flags that make it reachable, and nothing else ──────────
"$BIN" --remote-debugging-port="$PORT" --user-data-dir="$WORK/profile" \
       --no-first-run --no-default-browser-check about:blank >/dev/null 2>&1 &
CHROME_PID=$!
for _ in $(seq 1 60); do
  curl -sf --max-time 1 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -sf --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1 \
  || { say "FATAL: bare browser did not come up on :$PORT"; exit 1; }

say "== attached mode — a browser horse-browser never launched (:$PORT) =="

# hb <script> — the launcher's invocation, minus the launcher.
hb() { BU_CDP_URL="http://127.0.0.1:$PORT" BU_NAME="$BU" HORSE_SESSION="$SESS" \
       PYTHONPATH="$ROOT/harness" "$PY" -m horse_harness.run <<<"$1" 2>&1; }

# ── 0. the test is testing what it thinks it is ────────────────────────────────────
# NOT "zero service workers": a bare Chrome already runs Google's component extension
# (nkeimhogjdpnpccoofpliimaahmaaome). That is the point — its presence is exactly what made
# "the first chrome-extension:// worker" the wrong way to find ours. Assert instead that none
# of the workers here is OURS, which is the condition this whole suite is built on.
mine="$(hb 'print("MINE", ext_call("listTabsProbeThatDoesNotExist") is not None)' | sed -n 's/^MINE //p')"
[ "$mine" = "False" ] \
  && pass "bare browser carries no horse extension (component extensions notwithstanding)" \
  || fail "bare browser carries no horse extension" "ext_call resolved a worker: $mine"

# ── 1..4. the registry answers without an extension ────────────────────────────────
out="$(hb '
t1 = open_tab("data:text/html,<title>ATT-ONE</title>x"); wait_for_load()
t2 = open_tab("data:text/html,<title>ATT-TWO</title>x"); wait_for_load()
ids = [t["targetId"] for t in list_tabs()]
print("OPENED", t1 is not None and t2 is not None)
print("LISTED", t1 in ids and t2 in ids, len(ids))
print("TITLES", sorted(t.get("title") or "" for t in list_tabs()))
cdp("Target.closeTarget", targetId=t1)
print("PRUNED", t1 not in [t["targetId"] for t in list_tabs()])
')"
grep -q "OPENED True" <<<"$out" \
  && pass "open_tab works with no extension to group the tab" \
  || fail "open_tab works with no extension" "$out"
grep -q "LISTED True" <<<"$out" \
  && pass "list_tabs() names both tabs from the registry alone" \
  || fail "list_tabs() names both tabs" "$out"
grep -q "PRUNED True" <<<"$out" \
  && pass "list_tabs() drops a tab that was closed behind its back" \
  || fail "list_tabs() prunes a closed tab" "$out"
[ -s "$REG" ] && python3 -c "import json,sys; sys.exit(0 if json.load(open('$REG')) else 1)" 2>/dev/null \
  && pass "registry file written for this session (tabs/$BU)" \
  || fail "registry file written" "$REG missing or empty"

# ── 5. realness, JS half — applied per session by the daemon, not a content script ──
# On an https: page, not a data: URL: navigator.userAgentData is secure-context only, so a
# data: page reads as "no mask" whatever the mask is actually doing.
out="$(hb '
open_tab("https://example.com/"); wait_for_load()
r = realness_ok()
print("REAL", r["ok"], r["why"], r["brands"])
')"
grep -q "REAL True" <<<"$out" \
  && pass "realness JS half applied over CDP (brands claim Google Chrome)" \
  || fail "realness JS half applied over CDP" "$out"

# ── 6. realness, wire half — the header a bare browser would never send on its own ──
REFL_PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
python3 - "$REFL_PORT" >/dev/null 2>&1 <<'PY' &
import http.server, json, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        b = json.dumps({"sec_ch_ua": self.headers.get("sec-ch-ua"),
                        "ua": self.headers.get("user-agent")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
REFL_PID=$!
for _ in $(seq 1 20); do curl -sf "http://127.0.0.1:$REFL_PORT/" >/dev/null 2>&1 && break; sleep 0.2; done
out="$(REFL_PORT="$REFL_PORT" hb '
import json, os, re
open_tab("http://127.0.0.1:" + os.environ["REFL_PORT"] + "/"); wait_for_load()
seen = json.loads(js("document.body.textContent") or "{}")
major = (re.search(r"Chrome/(\d+)", seen.get("ua") or "") or [None, None])[1]
brands = dict(re.findall(r"\"([^\"]+)\";v=\"([^\"]+)\"", seen.get("sec_ch_ua") or ""))
print("WIRE", brands.get("Google Chrome") == major and major is not None, seen.get("sec_ch_ua"))
')"
grep -q "WIRE True" <<<"$out" \
  && pass "realness wire half applied over CDP (sec-ch-ua matches the UA major)" \
  || fail "realness wire half applied over CDP" "$out"

# ── 7. the reap path: the registry names what to close, no extension involved ───────
before="$(curl -s "http://127.0.0.1:$PORT/json/list" | python3 -c \
  "import json,sys; print(sum(1 for t in json.load(sys.stdin) if 'ATT-' in (t.get('title') or '')))")"
ids="$(python3 -c "import json; print(' '.join(json.load(open('$REG'))))" 2>/dev/null)"
for tid in $ids; do curl -s "http://127.0.0.1:$PORT/json/close/$tid" >/dev/null 2>&1; done
sleep 0.5
after="$(curl -s "http://127.0.0.1:$PORT/json/list" | python3 -c \
  "import json,sys; print(sum(1 for t in json.load(sys.stdin) if 'ATT-' in (t.get('title') or '')))")"
{ [ "${before:-0}" -ge 1 ] && [ "${after:-0}" -eq 0 ]; } \
  && pass "every tab this session opened is closable from the registry ($before->$after)" \
  || fail "registry names every tab this session opened" "ATT-* $before->$after"

say ""
say "== $PASS passed, $FAIL failed, $SKIP skipped =="
[ "$FAIL" -eq 0 ]
