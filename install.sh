#!/usr/bin/env bash
# One-time setup for horse-browser. Safe to re-run.
#
#   1. Fetches Chrome for Testing (a dedicated, automation-purposed browser that
#      coexists with your daily browser) via @puppeteer/browsers — you install
#      nothing by hand. Override with HORSE_BROWSER_BIN=/path/to/chromium to use
#      your own Chromium instead.
#   2. Writes config + symlinks the `horse-browser` launcher onto your PATH.
#   3. Registers statusline.sh in Claude Code settings (~/.claude/settings.json).
#   4. Launches the browser for the first time (sign into your apps — logins
#      persist), then smoke-tests the whole chain via browser-harness if present.
#
# Env overrides: HORSE_BROWSER_BIN, HORSE_BROWSER_PORT (9223),
# HORSE_BROWSER_PROFILE (~/.config/horse-browser/profile).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${HORSE_BROWSER_PORT:-9223}"
PROFILE="${HORSE_BROWSER_PROFILE:-$HOME/.config/horse-browser/profile}"
CONFIG_DIR="$HOME/.config/horse-browser"
CONFIG="$CONFIG_DIR/config"
CACHE="$HOME/.cache/horse-browser"
BINDIR="$HOME/.local/bin"
SETTINGS="$HOME/.claude/settings.json"
EXT="$HERE/extension"

mkdir -p "$CONFIG_DIR" "$BINDIR" "$CACHE"

# 1. browser ──────────────────────────────────────────────────────────────────
BIN="${HORSE_BROWSER_BIN:-}"
if [ -n "$BIN" ]; then
  echo "Using your nominated browser: $BIN"
else
  if ! command -v npx >/dev/null 2>&1; then
    echo "ERROR: npx (Node) not found — needed to fetch Chrome for Testing." >&2
    echo "  Install Node, or set HORSE_BROWSER_BIN to a Chromium binary, then re-run." >&2
    exit 1
  fi
  echo "Fetching Chrome for Testing via @puppeteer/browsers (one-time, ~170MB)…"
  out="$(npx -y @puppeteer/browsers install chrome@stable --path "$CACHE")"
  # output line: "chrome@<version> <path-to-executable>"  (path may contain spaces)
  BIN="$(printf '%s\n' "$out" | grep '^chrome@' | tail -1 | sed 's/^[^ ]* //')"
fi
if [ ! -x "$BIN" ]; then
  echo "ERROR: browser binary not found / not executable: $BIN" >&2
  exit 1
fi
echo "✓ browser: $BIN"

# 2. config + launcher on PATH ─────────────────────────────────────────────────
cat > "$CONFIG" <<EOF
# horse-browser config — written by install.sh
BROWSER_BIN="$BIN"
EXTENSION_DIR="$EXT"
PORT="$PORT"
PROFILE="$PROFILE"
EOF
ln -sf "$HERE/bin/horse-browser" "$BINDIR/horse-browser"
echo "✓ launcher: $BINDIR/horse-browser  (config: $CONFIG)"
case ":$PATH:" in *":$BINDIR:"*) ;; *) echo "  note: $BINDIR isn't on your PATH — add it so 'horse-browser' resolves";; esac

# 3. statusline ────────────────────────────────────────────────────────────────
if command -v jq >/dev/null 2>&1; then
  mkdir -p "$(dirname "$SETTINGS")"
  [ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"
  cp "$SETTINGS" "$SETTINGS.bak" 2>/dev/null || true
  tmp="$(mktemp)"
  jq --arg cmd "$HERE/statusline.sh" '.statusLine = {type:"command", command:$cmd}' "$SETTINGS" > "$tmp"
  mv "$tmp" "$SETTINGS"
  echo "✓ statusline registered in $SETTINGS (previous saved to $SETTINGS.bak)"
else
  echo "! jq not found — add to $SETTINGS:  \"statusLine\": {\"type\":\"command\",\"command\":\"$HERE/statusline.sh\"}"
fi

# 4. first launch + smoke test ─────────────────────────────────────────────────
# HORSE_SKIP_LAUNCH=1 skips this whole step — used by the "update" path (re-running
# install for a fresh pull) where relaunching the browser + a 40s smoke test would
# be noise. Steps 1–3 (browser fetch, config, launcher, statusline) still run.
if [ -n "${HORSE_SKIP_LAUNCH:-}" ]; then
  echo "✓ setup refreshed (skipped launch/smoke-test: HORSE_SKIP_LAUNCH set)"
  echo "  Restart to pick up changes:  horse-browser"
  exit 0
fi

echo "Launching for the first time…"
"$BINDIR/horse-browser" || true

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
  echo "Verifying through browser-harness…"
  verified=""
  for _ in $(seq 1 20); do   # ~40s — must clear the SW's first 30s keepalive tick
    if printf '%s' "$check" | browser-harness 2>/dev/null | grep -q READY; then verified=1; break; fi
    sleep 2
  done
  if [ -n "$verified" ]; then
    echo "✓ verified — listTabs() answered over CDP; the extension is live"
  else
    echo "! couldn't confirm the extension within ~25s — check the browser window."
  fi
fi

echo
echo "Next:"
echo "  • Sign into the apps you want your agents to use — logins persist in $PROFILE"
echo "  • Point CDP clients at it:  export BU_CDP_URL=http://127.0.0.1:$PORT"
echo "  • (Re)launch anytime — agents too — with:  horse-browser"
