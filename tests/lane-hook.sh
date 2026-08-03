#!/usr/bin/env bash
# tests/lane-hook.sh — the Claude Code subagent lane hook stays wired.
#
# This suite exists because the hook was silently absent for two days and nothing noticed.
# Three independent causes, one test each:
#   • install.sh skips wiring under HORSE_FROM_NPM, so the npm install never wired it
#   • nothing re-checked, so anything that removed the entry was permanent
#   • the entry stores an ABSOLUTE path, so moving the install left every Claude session
#     running a Bash hook that no longer exists — the failure mode with no symptom
#
# Everything runs against a temp settings.json via HORSE_BROWSER_CLAUDE_SETTINGS. The real
# ~/.claude/settings.json is never read or written here.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
HB="$ROOT/bin/horse-browser"
WIRE="$ROOT/tools/lane_hook_wire.py"
HOOK="$ROOT/integrations/claude-code/lane-hook.sh"
PASS=0; FAIL=0
say()  { printf '%s\n' "$*"; }
pass() { PASS=$((PASS+1)); say "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); say "  ✗ $1${2:+ — $2}"; }

WORK="$(mktemp -d -t hb-lanehook.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# cmds <file> <event> — the hook commands wired for an event, one per line
cmds() { python3 - "$1" "$2" <<'PY'
import json, sys
try: d = json.load(open(sys.argv[1]))
except Exception: sys.exit(0)
for e in (d.get("hooks", {}) or {}).get(sys.argv[2], []) or []:
    for h in e.get("hooks", []) or []:
        print(h.get("command", ""))
PY
}
wire() { python3 "$WIRE" "$HOOK" "$1" 2>&1; }

say "horse-browser lane-hook"

# ── 1. wires into a settings.json that has none ────────────────────────────────────
S="$WORK/empty.json"; echo '{}' > "$S"
out="$(wire "$S")"
{ [ "$(cmds "$S" PreToolUse)" = "$HOOK" ] && [ "$(cmds "$S" SubagentStop)" = "$HOOK" ]; } \
  && pass "wires both PreToolUse and SubagentStop into a bare settings.json" \
  || fail "wires both events" "pre=$(cmds "$S" PreToolUse) stop=$(cmds "$S" SubagentStop)"
grep -q "wired" <<<"$out" && pass "says so on stderr when it changes something" \
  || fail "announces a change" "$out"

# ── 2. idempotent, and SILENT the second time ──────────────────────────────────────
before="$(cat "$S")"
out="$(wire "$S")"
[ "$(cat "$S")" = "$before" ] && pass "second run is a byte-for-byte no-op" \
  || fail "second run is a no-op" "file changed"
[ -z "$out" ] && pass "silent when already correct (a launch stays quiet)" \
  || fail "silent when already correct" "$out"

# ── 3. THE regression: repairs a path that no longer exists ────────────────────────
# npm package removed / checkout moved. Left alone this fires on every Bash call in every
# Claude session and fails, with nothing to see.
S="$WORK/stale.json"
python3 - "$S" <<'PY'
import json, sys
gone = "/nonexistent/old/install/integrations/claude-code/lane-hook.sh"
json.dump({"hooks": {
    "PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": gone, "timeout": 10}]}],
    "SubagentStop": [{"hooks": [
        {"type": "command", "command": gone, "timeout": 30, "async": True}]}],
}}, open(sys.argv[1], "w"), indent=2)
PY
out="$(wire "$S")"
{ [ "$(cmds "$S" PreToolUse)" = "$HOOK" ] && [ "$(cmds "$S" SubagentStop)" = "$HOOK" ]; } \
  && pass "repairs a stale hook path in place (npm → dev symlink)" \
  || fail "repairs a stale hook path" "pre=$(cmds "$S" PreToolUse)"
[ "$(cmds "$S" PreToolUse | wc -l | tr -d ' ')" = "1" ] \
  && pass "repairs rather than appending a duplicate" \
  || fail "no duplicate after repair" "$(cmds "$S" PreToolUse)"
grep -q "repaired" <<<"$out" && pass "reports the repair" || fail "reports the repair" "$out"

# ── 4. a stale path belonging to SOMEONE ELSE is left alone ────────────────────────
# Only *lane-hook.sh entries are ours to touch. Rewriting another tool's broken hook to
# point at ours would be a far worse bug than the one we are fixing.
S="$WORK/foreign.json"
python3 - "$S" <<'PY'
import json, sys
json.dump({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
    {"type": "command", "command": "/nonexistent/other-tool/some-hook.sh", "timeout": 5}]}]}},
    open(sys.argv[1], "w"), indent=2)
PY
wire "$S" >/dev/null
grep -q "other-tool/some-hook.sh" <<<"$(cmds "$S" PreToolUse)" \
  && pass "never rewrites another tool's hook, even a broken one" \
  || fail "leaves a foreign hook alone" "$(cmds "$S" PreToolUse)"

# ── 5. unrelated settings survive ──────────────────────────────────────────────────
S="$WORK/rich.json"
cat > "$S" <<'JSON'
{ "model": "claude-fable-5[1m]", "permissions": { "allow": ["Bash(ls:*)"] },
  "hooks": { "PreToolUse": [ { "matcher": "Read",
    "hooks": [ { "type": "command", "command": "/some/other/hook.sh" } ] } ] } }
JSON
wire "$S" >/dev/null
python3 - "$S" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["model"] == "claude-fable-5[1m]", "model lost"
assert d["permissions"]["allow"] == ["Bash(ls:*)"], "permissions lost"
pre = d["hooks"]["PreToolUse"]
assert any(e.get("matcher") == "Read" for e in pre), "the operator's own Read hook was dropped"
PY
[ $? -eq 0 ] && pass "preserves model, permissions and other hooks" \
             || fail "preserves unrelated settings"

# ── 6. a corrupt settings.json is never overwritten ────────────────────────────────
S="$WORK/corrupt.json"; printf '{ this is not json' > "$S"
before="$(cat "$S")"
out="$(wire "$S")"
[ "$(cat "$S")" = "$before" ] && pass "refuses to overwrite an unparseable settings.json" \
  || fail "leaves a corrupt settings.json alone" "file was rewritten"
grep -qi "unreadable" <<<"$out" && pass "says why it did nothing" || fail "explains the bail" "$out"
[ ! -e "$S.hb-tmp" ] && pass "no .hb-tmp left behind (atomic write)" || fail "no temp file left"

# ── 7. the LAUNCHER self-heals — the whole point ───────────────────────────────────
# install.sh can't: it skips this under HORSE_FROM_NPM and only ever runs at install time.
S="$WORK/launcher.json"; echo '{}' > "$S"
HORSE_BROWSER_CLAUDE_SETTINGS="$S" HORSE_BROWSER_LANE_HOOK_STAMP="$WORK/stamp" \
  HORSE_BROWSER_LANE_HOOK_INTERVAL=0 "$HB" status >/dev/null 2>&1
[ "$(cmds "$S" PreToolUse)" = "$HOOK" ] \
  && pass "a plain horse-browser run wires the hook (npm install path covered)" \
  || fail "launcher self-heals the hook" "pre=$(cmds "$S" PreToolUse)"

# ── 8. opt-out is honoured ─────────────────────────────────────────────────────────
S="$WORK/optout.json"; echo '{}' > "$S"
HORSE_BROWSER_NO_LANE_HOOK=1 HORSE_BROWSER_CLAUDE_SETTINGS="$S" \
  HORSE_BROWSER_LANE_HOOK_STAMP="$WORK/stamp2" HORSE_BROWSER_LANE_HOOK_INTERVAL=0 \
  "$HB" status >/dev/null 2>&1
[ "$(cat "$S")" = '{}' ] && pass "HORSE_BROWSER_NO_LANE_HOOK=1 leaves settings untouched" \
  || fail "opt-out honoured" "$(cat "$S")"

# ── 9. throttled — not re-parsing settings.json on every single call ───────────────
S="$WORK/throttle.json"; echo '{}' > "$S"; ST="$WORK/stamp3"
HORSE_BROWSER_CLAUDE_SETTINGS="$S" HORSE_BROWSER_LANE_HOOK_STAMP="$ST" \
  HORSE_BROWSER_LANE_HOOK_INTERVAL=0 "$HB" status >/dev/null 2>&1   # arms + wires
python3 - "$S" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d["hooks"] = {}          # strip it back out
json.dump(d, open(sys.argv[1], "w"), indent=2)
PY
HORSE_BROWSER_CLAUDE_SETTINGS="$S" HORSE_BROWSER_LANE_HOOK_STAMP="$ST" \
  "$HB" status >/dev/null 2>&1                                       # default 3600s: skip
[ -z "$(cmds "$S" PreToolUse)" ] \
  && pass "skips while the stamp is fresh (no settings.json parse per call)" \
  || fail "throttle honoured" "re-wired inside the interval"

say ""
say "── $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
