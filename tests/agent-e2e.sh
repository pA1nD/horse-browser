#!/usr/bin/env bash
# tests/agent-e2e.sh — a REAL agent (headless `claude -p`) drives horse-browser to fulfill a
# goal, proving the WHOLE stack works end to end: the rules/skill teach the agent about
# horse-browser, it writes a driver script, the launcher brings the browser up focus-safely,
# and the agent reads the live page. Two checks: the agent actually INVOKED horse-browser
# (not curl / a fetch tool), and it returned the CORRECT answer.
#
# On-demand — it spawns an LLM agent (non-deterministic, costs tokens, needs the claude CLI),
# so it is NOT part of `npm test`. Run it to validate the real agent↔browser integration.
set -u

command -v claude >/dev/null 2>&1 || { echo "FATAL: claude CLI not found (needed for the agent)"; exit 1; }

GOAL="Use the horse-browser command-line tool to open https://example.com and tell me the page's main heading. horse-browser is driven with a heredoc, e.g.:
horse-browser <<'PY'
tid = bh_open('https://example.com')
wait_for_load()
print(page_info().get('title'))
print(js('document.querySelector(\"h1\").innerText'))
PY
Reply with ONLY the exact text of the page's <h1> element — no explanation."
EXPECT="Example Domain"
STREAM="$(mktemp /tmp/agent-e2e.XXXXXX.jsonl)"
trap 'rm -f "$STREAM"' EXIT

echo "agent-e2e: a headless agent will read example.com via horse-browser (LLM call, ~30-90s)…"
timeout 300 claude -p "$GOAL" \
  --allowedTools "Bash" --output-format stream-json --verbose < /dev/null > "$STREAM" 2>/dev/null

FAIL=0
out="$(python3 - "$STREAM" "$EXPECT" <<'PY'
import json, sys
stream, expect = sys.argv[1], sys.argv[2]
used_hb = False; answer = ""
for line in open(stream):
    try: ev = json.loads(line)
    except Exception: continue
    for block in ((ev.get("message") or {}).get("content") or []):
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash":
            if "horse-browser" in (block.get("input") or {}).get("command", ""):
                used_hb = True
    if ev.get("type") == "result":
        answer = (ev.get("result") or "").strip()
ok_hb = "1" if used_hb else "0"
ok_ans = "1" if expect.lower() in answer.lower() else "0"
print(ok_hb, ok_ans, answer[:80].replace("\n"," "))
PY
)"
read -r OK_HB OK_ANS ANSWER <<<"$out"

echo ""
[ "$OK_HB" = "1" ]  && { echo "  ✓ the agent actually invoked horse-browser"; } \
                    || { echo "  ✗ the agent did NOT invoke horse-browser (used something else)"; FAIL=1; }
[ "$OK_ANS" = "1" ] && { echo "  ✓ the agent returned the correct answer via the browser ($ANSWER)"; } \
                    || { echo "  ✗ wrong/empty answer: $ANSWER"; FAIL=1; }

echo ""
if [ "${FAIL:-0}" = 0 ]; then echo "── agent-e2e PASSED"; exit 0
else echo "── agent-e2e FAILED"; exit 1; fi
