#!/usr/bin/env bash
# One-time setup for test-brave. Safe to re-run.
#
#   1. Registers statusline.sh in your Claude Code settings (~/.claude/settings.json)
#      so the statusline shows ses:XXXX — the same label the tab grouper uses.
#   2. Launches the dedicated Brave profile for the first time, with the Agent
#      Tab Grouper loaded, so you can sign into the apps you want your agents to
#      use. Those logins persist in the profile.
#
# Env overrides: TEST_BRAVE_PORT (default 9223), TEST_BRAVE_PROFILE
# (default ~/.config/test-brave).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${TEST_BRAVE_PORT:-9223}"
PROFILE="${TEST_BRAVE_PROFILE:-$HOME/.config/test-brave}"
SETTINGS="$HOME/.claude/settings.json"

# 1. statusline ──────────────────────────────────────────────────────────────
if command -v jq >/dev/null 2>&1; then
  mkdir -p "$(dirname "$SETTINGS")"
  [ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"
  cp "$SETTINGS" "$SETTINGS.bak" 2>/dev/null || true   # one backup before we touch it
  tmp="$(mktemp)"
  jq --arg cmd "$HERE/statusline.sh" \
     '.statusLine = {type: "command", command: $cmd}' "$SETTINGS" > "$tmp"
  mv "$tmp" "$SETTINGS"
  echo "✓ statusline registered in $SETTINGS (previous saved to $SETTINGS.bak)"
else
  echo "! jq not found — skipping statusline. Install jq, then add to $SETTINGS:"
  echo "    \"statusLine\": { \"type\": \"command\", \"command\": \"$HERE/statusline.sh\" }"
fi

# 2. first launch ─────────────────────────────────────────────────────────────
if [ ! -d "/Applications/Brave Browser.app" ]; then
  echo "ERROR: Brave Browser not found at /Applications/Brave Browser.app" >&2
  exit 1
fi
open -na "Brave Browser" --args \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE" \
  --load-extension="$HERE/extension" \
  --no-first-run --no-default-browser-check
echo "✓ launched Brave — profile: $PROFILE, CDP :$PORT, Agent Tab Grouper loaded"

# 3. smoke test (best-effort; only if browser-harness is installed) ───────────
# Brave is up — drive a real listTabs() call through the extension over CDP to
# confirm the whole chain (Brave ⇄ CDP ⇄ extension service worker) works, not
# just that Brave booted.
if command -v browser-harness >/dev/null 2>&1; then
  export BU_CDP_URL="http://127.0.0.1:$PORT"
  read -r -d '' check <<'PY' || true
from browser_harness.helpers import cdp
sw = next((t["targetId"] for t in cdp("Target.getTargets")["targetInfos"]
           if t.get("type") == "service_worker"
           and t.get("url", "").startswith("chrome-extension://")), None)
if not sw:
    print("PENDING"); raise SystemExit(0)
s = cdp("Target.attachToTarget", targetId=sw, flatten=True)["sessionId"]
r = cdp("Runtime.evaluate", session_id=s,
        expression="self.listTabs ? self.listTabs('__install_check__') : 'NOFN'",
        awaitPromise=True, returnByValue=True)
cdp("Target.detachFromTarget", sessionId=s)
print("READY" if isinstance(r.get("result", {}).get("value"), list) else "PENDING")
PY
  echo "Verifying through browser-harness (waiting for Brave to come up)…"
  verified=""
  for _ in $(seq 1 12); do
    if printf '%s' "$check" | browser-harness 2>/dev/null | grep -q READY; then
      verified=1; break
    fi
    sleep 2
  done
  if [ -n "$verified" ]; then
    echo "✓ verified — listTabs() answered over CDP; the extension is live"
  else
    echo "! couldn't confirm within ~25s — Brave may still be booting, or the"
    echo "  extension is disabled. Check brave://extensions, then re-run."
  fi
fi

echo
echo "Next:"
echo "  • Sign into the apps you want your agents to use — those logins persist."
echo "  • Point your CDP client at it:  export BU_CDP_URL=http://127.0.0.1:$PORT"
