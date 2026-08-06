#!/usr/bin/env bash
# tests/external-state.sh — the three files horse-browser owns outside its own directories
# stay exactly right, and nothing else does.
#
#   ~/.claude/settings.json           lane-hook entries (a file we SHARE with the operator)
#   ~/.grok/hooks/horse-browser.json  the grok session hook (ours outright)
#   ~/.claude/rules/horse-browser.md  the rule (ours outright, on by default)
#
# The suite exists because each of those went wrong in a different way:
#   • the lane hook APPENDED instead of converging — ten registrations were found on the
#     operator's machine where one belongs, one per checkout that had ever run the launcher
#   • a checkout under $TMPDIR wired its own path into the operator's global settings, was
#     cleaned up, and left every Bash call in every Claude Code session failing a hook
#   • the rule was written only by an explicit command, so it froze five releases behind
#
# Everything runs against temp paths. The operator's real ~/.claude and ~/.grok are never
# read or written — if that stops being true, this suite is the thing that would do it.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
HB="$ROOT/bin/horse-browser"
HOOK="$ROOT/integrations/claude-code/lane-hook.sh"
PASS=0; FAIL=0
say()  { printf '%s\n' "$*"; }
pass() { PASS=$((PASS+1)); say "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); say "  ✗ $1${2:+ — $2}"; }

WORK="$(mktemp -d -t hb-extstate.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

CLAUDE="$WORK/claude"; GROK="$WORK/grok"
S="$CLAUDE/settings.json"; RULE_MD="$CLAUDE/rules/horse-browser.md"; STAMP="$WORK/stamp"
mkdir -p "$CLAUDE/rules" "$GROK"

# run [extra env...] — one launcher invocation against the temp targets. `status` is used
# because it exits before touching a browser, so no test here needs Chrome.
run() {
  env HORSE_BROWSER_CLAUDE_SETTINGS="$S" HORSE_BROWSER_GROK_HOOKS="$GROK" \
      HORSE_BROWSER_RULES_MD="$RULE_MD" HORSE_BROWSER_RECONCILE_STAMP="$STAMP" \
      "$@" "$HB" status 2>&1 >/dev/null | grep -E "wired|collapsed|repointed|wrote|refreshed|removed|unreadable" || true
}
# cmds <event> — the hook commands wired for an event, one per line
cmds() { python3 - "$S" "$1" <<'PY'
import json, sys
try: d = json.load(open(sys.argv[1]))
except Exception: sys.exit(0)
for e in (d.get("hooks", {}) or {}).get(sys.argv[2], []) or []:
    for h in e.get("hooks", []) or []:
        print(h.get("command", ""))
PY
}
reset() { rm -rf "$CLAUDE" "$GROK" "$STAMP"; mkdir -p "$CLAUDE/rules" "$GROK"; echo '{}' > "$S"; }

say "horse-browser external-state"

# ── 1. a fresh machine gets all three ──────────────────────────────────────────────
reset
out="$(run)"
[ "$(cmds PreToolUse | wc -l | tr -d ' ')" = 1 ] && [ "$(cmds SubagentStop | wc -l | tr -d ' ')" = 1 ] \
  && pass "wires both PreToolUse and SubagentStop" \
  || fail "wires both events" "pre=$(cmds PreToolUse)"
[ -f "$GROK/horse-browser.json" ] && pass "writes the grok session hook" || fail "writes the grok hook"
[ -f "$RULE_MD" ] && pass "writes the rule file — on by default, no command needed" \
  || fail "writes the rule by default"
grep -q "wired" <<<"$out" && pass "says on stderr what it changed" || fail "announces changes" "$out"

# ── 2. the rule is RULE.md verbatim, under a header that says who owns it ───────────
diff <(tail -n +4 "$RULE_MD") "$ROOT/RULE.md" >/dev/null \
  && pass "rule body is RULE.md byte-for-byte (no second source of truth)" \
  || fail "rule matches RULE.md" "$(diff <(tail -n +4 "$RULE_MD") "$ROOT/RULE.md" | head -3)"
head -1 "$RULE_MD" | grep -q "managed by @pa1nd/horse-browser" \
  && pass "rule header names its owner (the only uninstall clue npm leaves)" \
  || fail "rule header names its owner"

# ── 3. idempotent, and silent when it changes nothing ──────────────────────────────
before="$(cat "$S")"; rbefore="$(cat "$RULE_MD")"
out="$(run)"
{ [ "$(cat "$S")" = "$before" ] && [ "$(cat "$RULE_MD")" = "$rbefore" ]; } \
  && pass "second run is byte-for-byte identical" || fail "second run is a no-op"
[ -z "$out" ] && pass "silent when already correct (a launch stays quiet)" || fail "silent" "$out"

# ── 4. THE regression: duplicates collapse, they do not accumulate ─────────────────
# Ten entries is what the operator's machine actually had — one per checkout that had ever
# run the launcher, because the old repair rewrote a dead entry instead of removing it.
python3 - "$S" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["hooks"]["PreToolUse"] = [json.loads(json.dumps(d["hooks"]["PreToolUse"][0])) for _ in range(10)]
json.dump(d, open(sys.argv[1], "w"), indent=2)
PY
out="$(run)"
[ "$(cmds PreToolUse | wc -l | tr -d ' ')" = 1 ] \
  && pass "ten duplicate entries collapse to exactly one" \
  || fail "collapses duplicates" "$(cmds PreToolUse | wc -l) left"
grep -q "collapsed 10" <<<"$out" && pass "reports how many it collapsed" || fail "reports the collapse" "$out"

# ── 5. a stale path is replaced, not appended next to ──────────────────────────────
python3 - "$S" <<'PY'
import json, sys
gone = "/nonexistent/old/install/integrations/claude-code/lane-hook.sh"
d = json.load(open(sys.argv[1]))
d["hooks"]["PreToolUse"] = [{"matcher": "Bash", "hooks": [
    {"type": "command", "command": gone, "timeout": 10}]}]
json.dump(d, open(sys.argv[1], "w"), indent=2)
PY
run >/dev/null
{ [ "$(cmds PreToolUse | wc -l | tr -d ' ')" = 1 ] && cmds PreToolUse | grep -q "$ROOT"; } \
  && pass "a stale path is repointed in place, never duplicated" \
  || fail "repoints a stale path" "$(cmds PreToolUse)"

# ── 6. never touches another tool's hook, even a broken one ────────────────────────
python3 - "$S" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [
    {"type": "command", "command": "/nonexistent/other-tool/some-hook.sh", "timeout": 5}]})
json.dump(d, open(sys.argv[1], "w"), indent=2)
PY
run >/dev/null
cmds PreToolUse | grep -q "other-tool/some-hook.sh" \
  && pass "leaves a foreign hook alone, broken or not" || fail "foreign hook survives" "$(cmds PreToolUse)"

# ── 7. the operator's settings.json is theirs ──────────────────────────────────────
reset
cat > "$S" <<'JSON'
{ "model": "claude-fable-5[1m]", "permissions": { "allow": ["Bash(ls:*)"] },
  "hooks": { "PreToolUse": [ { "matcher": "Read",
    "hooks": [ { "type": "command", "command": "/some/other/hook.sh" } ] } ] } }
JSON
run >/dev/null
python3 - "$S" <<'PY' && pass "preserves model, permissions and unrelated hooks" \
                     || fail "preserves unrelated settings"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["model"] == "claude-fable-5[1m]", "model lost"
assert d["permissions"]["allow"] == ["Bash(ls:*)"], "permissions lost"
assert any(e.get("matcher") == "Read" for e in d["hooks"]["PreToolUse"]), "operator's Read hook dropped"
PY

# ── 8. a corrupt settings.json is never overwritten ────────────────────────────────
reset; printf '{ this is not json' > "$S"; before="$(cat "$S")"
out="$(run)"
[ "$(cat "$S")" = "$before" ] && pass "refuses to overwrite an unparseable settings.json" \
  || fail "leaves a corrupt settings.json alone"
grep -qi "unreadable" <<<"$out" && pass "says why it did nothing" || fail "explains the bail" "$out"
[ ! -e "$S.hb-tmp" ] && pass "no .hb-tmp left behind (atomic write)" || fail "no temp file left"

# ── 9. drift is repaired, even in the same second as the last run ──────────────────
# mtime at one-second resolution compared EQUAL here and the edit stayed invisible until the
# hourly ceiling — which is the whole reason the gate fingerprints sub-second mtime and size.
reset; run >/dev/null
python3 -c "
import json,sys; p='$S'; d=json.load(open(p)); d['hooks']={}; json.dump(d,open(p,'w'))"
run >/dev/null
[ "$(cmds PreToolUse | wc -l | tr -d ' ')" = 1 ] \
  && pass "a target edited in the same second is still noticed" \
  || fail "same-second drift detected" "not rewired"

rm -f "$RULE_MD"; run >/dev/null
[ -f "$RULE_MD" ] && pass "a deleted rule file comes back" || fail "deleted rule restored"

# ── 10. a changed RULE.md reaches the installed copy ───────────────────────────────
# The bug in the field: the operator's rule was applied once in July and never refreshed, so
# it was missing the paragraph telling agents not to idle on a bare sleep.
cp "$ROOT/RULE.md" "$WORK/RULE.md.bak"
printf '\n<!-- external-state probe -->\n' >> "$ROOT/RULE.md"
run >/dev/null
grep -q "external-state probe" "$RULE_MD" \
  && pass "an edited RULE.md propagates on the next run (no re-apply step)" \
  || fail "rule refresh reaches the installed copy"
cp "$WORK/RULE.md.bak" "$ROOT/RULE.md"
run >/dev/null
grep -q "external-state probe" "$RULE_MD" && fail "reverting RULE.md reverts the copy" \
  || pass "and reverting the source reverts the copy"

# ── 11. the toggle ─────────────────────────────────────────────────────────────────
CFG="$WORK/config"
toggle() { env HORSE_BROWSER_CLAUDE_SETTINGS="$S" HORSE_BROWSER_GROK_HOOKS="$GROK" \
  HORSE_BROWSER_RULES_MD="$RULE_MD" HORSE_BROWSER_RECONCILE_STAMP="$STAMP" \
  HOME="$WORK/fakehome" "$HB" rule "$1" 2>&1; }
mkdir -p "$WORK/fakehome/.config/horse-browser"
toggle off >/dev/null
[ ! -e "$RULE_MD" ] && pass "'rule off' removes the file (no stale instructions left behind)" \
  || fail "rule off removes it"
grep -q '^RULE="0"' "$WORK/fakehome/.config/horse-browser/config" \
  && pass "'rule off' persists to the config" || fail "rule off persists"
toggle on >/dev/null
[ -f "$RULE_MD" ] && pass "'rule on' brings it back" || fail "rule on restores it"

# ── 12. a hook whose package was uninstalled is a NO-OP, not an error ──────────────
# npm fires no uninstall hook (verified: neither preuninstall nor postuninstall runs for
# `npm rm -g`), so the entry outlives the package. Unguarded, that is an error on every
# single Bash call in every session — the exact failure that prompted this suite.
reset; run >/dev/null
cmd="$(cmds PreToolUse)"
echo '{}' | sh -c "$cmd" >/dev/null 2>&1
[ $? -eq 0 ] && pass "the registered command runs clean while installed" || fail "hook runs while installed"
echo '{}' | sh -c "${cmd//$ROOT/\/nonexistent\/removed}" >/dev/null 2>&1
[ $? -eq 0 ] && pass "…and exits 0 silently once the package is gone" \
  || fail "uninstalled package is a no-op" "non-zero exit would fail every Bash call"

# ── 13. a checkout in a temp dir must never claim global config ────────────────────
# A build agent's throwaway clone did exactly this to the operator's real settings.json.
TMPTREE="$WORK/tmproot/horse-browser"
mkdir -p "$TMPTREE"
for d in bin tools integrations RULE.md; do cp -R "$ROOT/$d" "$TMPTREE/"; done
reset
env HORSE_BROWSER_CLAUDE_SETTINGS="$S" HORSE_BROWSER_GROK_HOOKS="$GROK" \
    HORSE_BROWSER_RULES_MD="$RULE_MD" HORSE_BROWSER_RECONCILE_STAMP="$STAMP" \
    TMPDIR="$WORK" "$TMPTREE/bin/horse-browser" status >/dev/null 2>&1
{ [ "$(cat "$S")" = '{}' ] && [ ! -e "$RULE_MD" ]; } \
  && pass "a \$ROOT under TMPDIR writes nothing at all" \
  || fail "temp-dir checkout refused" "it wrote to the operator's config"

# ── 14. the opt-out, which every other suite relies on ─────────────────────────────
reset
run HORSE_BROWSER_NO_RECONCILE=1 >/dev/null
{ [ "$(cat "$S")" = '{}' ] && [ ! -e "$RULE_MD" ] && [ ! -e "$GROK/horse-browser.json" ]; } \
  && pass "HORSE_BROWSER_NO_RECONCILE=1 touches nothing" \
  || fail "opt-out honoured" "$(cat "$S")"

# ── 15. grok's config dir is detected, never created ───────────────────────────────
reset
env HORSE_BROWSER_CLAUDE_SETTINGS="$S" HORSE_BROWSER_GROK_HOOKS="$WORK/no-grok-here/hooks" \
    HORSE_BROWSER_RULES_MD="$RULE_MD" HORSE_BROWSER_RECONCILE_STAMP="$STAMP" \
    "$HB" status >/dev/null 2>&1
[ ! -d "$WORK/no-grok-here" ] \
  && pass "no ~/.grok means grok is not installed — we don't conjure it" \
  || fail "never creates a missing agent-system config dir"

say ""
say "── $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
