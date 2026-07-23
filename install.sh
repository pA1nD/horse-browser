#!/usr/bin/env bash
# One-time setup for horse-browser. Safe to re-run.
#
#   1. Fetches Chrome for Testing (a dedicated, automation-purposed browser that
#      coexists with your daily browser) via @puppeteer/browsers — you install
#      nothing by hand. Override with HORSE_BROWSER_BIN=/path/to/chromium to use
#      your own Chromium instead.
#   2. Writes config + symlinks the `horse-browser` launcher onto your PATH.
#   3. Launches the browser for the first time (sign into your apps — logins
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
EXT="$HERE/extension"

mkdir -p "$CONFIG_DIR" "$BINDIR" "$CACHE"

# 0. harness venv ───────────────────────────────────────────────────────────────
# The vendored horse-harness (harness/horse_harness) drives the browser — no separate
# browser-harness install needed. Build its private venv now so the first driver call
# doesn't pay the setup. Checked before the Chrome fetch so we don't pull 170MB only
# to bail on a missing Python.
"$HERE/bin/horse-browser" harness-setup || exit 1
echo "✓ harness: vendored horse-harness ready ($HERE/harness)"

# Our own SKILL.md ships in the package dir — under `npm -g` that's a volatile Node-version
# path (…/fnm/node-versions/vX/…). Copy it to a stable ~/.config home the rule file's
# @-import points at. REQUIRED, and done here — before the Chrome fetch (which may degrade) — so it
# always exists; set -e aborts the install if the copy can't be written.
"$HERE/claude-md.sh" skill

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
# When installed via npm (`npm i -g @pa1nd/horse-browser`), npm owns the launcher
# symlink (package.json "bin") — don't lay down a second, competing one in ~/.local/bin.
if [ -n "${HORSE_FROM_NPM:-}" ]; then
  echo "✓ launcher: managed by npm (bin: horse-browser)  (config: $CONFIG)"
else
  ln -sf "$HERE/bin/horse-browser" "$BINDIR/horse-browser"
  echo "✓ launcher: $BINDIR/horse-browser  (config: $CONFIG)"
  case ":$PATH:" in *":$BINDIR:"*) ;; *) echo "  note: $BINDIR isn't on your PATH — add it so 'horse-browser' resolves";; esac
fi

# 3. workspace migration — retire the old loader stub ───────────────────────────
# The bh_open/trusted-input helpers are FOLDED INTO the vendored harness now
# (harness/horse_harness/helpers.py + input.py). Older installs synced them into the
# browser-harness workspace as horse_helpers.py/horse_input.py plus a loader stub in
# agent_helpers.py — retire all of that so stale copies can't shadow the packaged
# versions. Everything the user keeps in agent_helpers.py is preserved byte-for-byte
# (the harness still loads that file and pre-seeds it with every public helper).
LOADER_MARKER="# >>> horse-browser: bh_open helpers (managed loader — do not edit) >>>"
migrate_workspace() {
  local ws="$1" dst="$1/agent_helpers.py"
  [ -d "$ws" ] || return 0
  rm -f "$ws/horse_helpers.py" "$ws/horse_input.py"
  if [ -f "$dst" ] && grep -qF "$LOADER_MARKER" "$dst"; then
    python3 - "$dst" <<'PY' || true
import re, sys
p = sys.argv[1]
text = open(p).read()
pat = re.compile(
    r"\n*# >>> horse-browser: bh_open helpers \(managed loader — do not edit\) >>>"
    r".*?# <<< horse-browser: bh_open helpers <<<\n*",
    re.S)
new = pat.sub("\n\n", text, count=1).strip("\n")
if new != text:
    open(p, "w").write(new + ("\n" if new else ""))
    print("  migrated: retired the horse_helpers loader stub (helpers ship in the harness now)")
PY
  fi
}
migrate_workspace "${BH_AGENT_WORKSPACE:-$HOME/.config/browser-harness/agent-workspace}"
for c in "$HOME/Developer/browser-harness" "$HOME/browser-harness"; do
  migrate_workspace "$c/agent-workspace"
done
echo "✓ helpers: folded into the vendored harness (workspace agent_helpers.py still loads for your own additions)"

# Keep the stable ~/.config copy of the vendored harness SKILL current, so the rule
# file's @-import never rots across npm/Node reinstalls. Writes only ~/.config (never
# ~/.claude). Registering the rule file (~/.claude/rules/horse-browser.md) stays opt-in
# via ./claude-md.sh apply; the npm next-steps note points the user at it.
"$HERE/claude-md.sh" symlink || echo "  (skipped harness SKILL copy — see ./claude-md.sh)" >&2

# Wire the claude-code lane hook into ~/.claude/settings.json: PreToolUse gives each
# SUBAGENT's horse-browser calls their own lane (own daemon + tab group — parallel
# subagents stop clobbering each other); SubagentStop cleans that lane up. Pure harness
# plumbing — agents never see it. Idempotent: skipped if the hook is already wired.
# Same npm policy as above: never touch ~/.claude from a silent postinstall.
if [ -z "${HORSE_FROM_NPM:-}" ]; then
  python3 - "$HERE/integrations/claude-code/lane-hook.sh" "$HOME/.claude/settings.json" <<'PY' || \
    echo "  (couldn't wire the lane hook into ~/.claude/settings.json — subagents will share the session's browser lane)" >&2
import json, os, sys
hook, path = sys.argv[1], sys.argv[2]
d = {}
if os.path.exists(path):
    d = json.load(open(path))
hooks = d.setdefault("hooks", {})
def wired(event):
    return any(h.get("command") == hook
               for e in hooks.get(event, []) for h in e.get("hooks", []))
changed = False
if not wired("PreToolUse"):
    hooks.setdefault("PreToolUse", []).append(
        {"matcher": "Bash", "hooks": [{"type": "command", "command": hook, "timeout": 10}]})
    changed = True
if not wired("SubagentStop"):
    hooks.setdefault("SubagentStop", []).append(
        {"hooks": [{"type": "command", "command": hook, "timeout": 30, "async": True}]})
    changed = True
if changed:
    json.dump(d, open(path, "w"), indent=2)
    print("✓ wired the subagent lane hook into ~/.claude/settings.json (new sessions pick it up)")
else:
    print("✓ subagent lane hook already wired into ~/.claude/settings.json")
PY
fi

# 4. first launch + smoke test ─────────────────────────────────────────────────
# HORSE_SKIP_LAUNCH=1 skips this whole step — used by the "update" path (re-running
# install for a fresh pull) where relaunching the browser + a 40s smoke test would
# be noise. Steps 1–3 (browser fetch, config, launcher, helpers) still run.
if [ -n "${HORSE_SKIP_LAUNCH:-}" ]; then
  echo "✓ setup refreshed (skipped launch/smoke-test: HORSE_SKIP_LAUNCH set)"
  echo "  Restart to pick up changes:  horse-browser"
  exit 0
fi

echo "Launching for the first time…"
"$BINDIR/horse-browser" || true

read -r -d '' check <<'PY' || true
# bh_open + trusted input are folded into the harness — both must be pre-imported.
if "bh_open" not in globals() or "type_into" not in globals():
    print("NOHELPERS"); raise SystemExit(0)
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
echo "Verifying through the vendored harness…"
verified=""; result=""
for _ in $(seq 1 20); do   # ~40s — must clear the SW's first 30s keepalive tick
  result="$(printf '%s' "$check" | "$HERE/bin/horse-browser" 2>/dev/null | tail -1)"
  [ "$result" = "READY" ] && { verified=1; break; }
  [ "$result" = "NOHELPERS" ] && break   # helpers not loading — no point retrying
  sleep 2
done
if [ -n "$verified" ]; then
  echo "✓ verified — bh_open + trusted input loaded, listTabs() answered over CDP; the extension is live"
elif [ "$result" = "NOHELPERS" ]; then
  echo "! helpers did NOT load — the vendored harness looks broken; re-run: horse-browser harness-setup" >&2
else
  echo "! couldn't confirm the extension within ~40s — check the browser window."
fi

echo
echo "Next:"
echo "  • Sign into the apps you want your agents to use — logins persist in $PROFILE"
echo "  • Point CDP clients at it:  export BU_CDP_URL=http://127.0.0.1:$PORT"
echo "  • (Re)launch anytime — agents too — with:  horse-browser"
