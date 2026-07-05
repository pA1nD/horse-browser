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

# 3. bh_open helpers into browser-harness ───────────────────────────────────────
# browser-harness auto-loads <workspace>/agent_helpers.py on every call. Our helpers
# ship as horse_helpers.py — a file THIS installer owns and overwrites outright on
# every sync — plus a load-once stub appended to agent_helpers.py exactly once and
# never rewritten, so anything a user adds to agent_helpers.py is never touched.
# The workspace location varies by version/install (≤0.1.0: <repo>/agent-workspace;
# 0.1.1+: ~/.config/browser-harness/agent-workspace; or whatever BH_AGENT_WORKSPACE
# points at) — so instead of guessing, we ASK browser-harness itself where it loads
# from via its own python (helpers.AGENT_WORKSPACE).
if ! command -v browser-harness >/dev/null 2>&1; then
  # browser-harness is a separate (Python) prerequisite. In a hand/curl install it's
  # required up front. Under npm it's a *declared* prereq the user may not have yet — so
  # warn and finish cleanly (config + Chrome are already in place; re-run setup once
  # browser-harness is installed to get the bh_open helpers) rather than failing the install.
  echo "! browser-harness not found on PATH — skipping the bh_open helper sync." >&2
  echo "  Install it (e.g. 'uv tool install browser-harness'), then re-run setup." >&2
  [ -n "${HORSE_FROM_NPM:-}" ] && exit 0
  echo "  (https://github.com/browser-use/browser-harness)" >&2
  exit 1
fi
HELPERS_SRC="$HERE/agent-helpers.py"
INPUT_SRC="$HERE/agent-input.py"   # Tier 2 trusted-input layer → workspace/horse_input.py
# Legacy marker: pre-0.4.1 installs appended the helpers INLINE under this line, and
# re-syncs replaced marker→EOF — silently eating anything a user had added below the
# block. Kept only so those files can be migrated once.
HELPERS_MARKER="# ── horse-browser helpers (installed by horse-browser/install.sh) ──"
LOADER_MARKER="# >>> horse-browser: bh_open helpers (managed loader — do not edit) >>>"
install_helpers_into() {  # $1 = workspace dir; (re)syncs the helpers — idempotent, so
  local ws="$1" dst="$1/agent_helpers.py"  # re-running install.sh deploys helper UPDATES too.
  mkdir -p "$ws" 2>/dev/null || return 0
  cp "$HELPERS_SRC" "$ws/horse_helpers.py"
  cp "$INPUT_SRC" "$ws/horse_input.py"   # loaded by horse_helpers.py (chain-exec)
  # one-time migration of a legacy inline block: strip it ONLY on an exact byte match
  # with the shipped source, so user additions below it survive. A modified/unknown
  # block is left in place — harmless, the loader runs after it and its defs win.
  if [ -f "$dst" ] && grep -qF "$HELPERS_MARKER" "$dst"; then
    python3 - "$dst" "$HELPERS_SRC" "$HELPERS_MARKER" <<'PY' || true
import sys
dst, src, marker = sys.argv[1], sys.argv[2], sys.argv[3]
text, block = open(dst).read(), open(src).read()
i = text.find(marker)
legacy = marker + "\n" + block
if i >= 0 and text[i:].startswith(legacy):
    head = text[:i].rstrip("\n")
    rest = text[i + len(legacy):].lstrip("\n")
    parts = [p for p in (head, rest) if p]
    open(dst, "w").write("\n\n".join(parts) + ("\n" if parts else ""))
    print("  migrated: legacy inline helper block removed (everything else kept)")
elif i >= 0:
    print("  note: a legacy horse-browser block is present but doesn't match the shipped", file=sys.stderr)
    print("        source — left in place (the horse_helpers.py loader below supersedes", file=sys.stderr)
    print("        it); remove it from " + dst + " by hand when convenient.", file=sys.stderr)
PY
  fi
  if ! grep -qF "$LOADER_MARKER" "$dst" 2>/dev/null; then
    if [ -s "$dst" ]; then printf '\n' >> "$dst"; fi
    printf '%s\n' "$LOADER_MARKER" >> "$dst"
    cat >> "$dst" <<'LOADER'
# The helpers live in horse_helpers.py next to this file. install.sh overwrites THAT
# file on every sync and never rewrites this one — your own code here is safe.
try:
    import os as _hb_os
    _hb_path = _hb_os.path.join(_hb_os.path.dirname(_hb_os.path.abspath(__file__)), "horse_helpers.py")
    exec(compile(open(_hb_path).read(), _hb_path, "exec"))
except Exception as _hb_err:
    import sys as _hb_sys
    print("horse-browser: couldn't load horse_helpers.py (%r) — re-run horse-browser's install.sh" % (_hb_err,), file=_hb_sys.stderr)
# <<< horse-browser: bh_open helpers <<<
LOADER
  fi
  echo "✓ synced bh_open helpers → $ws/horse_helpers.py (loaded via stub in $dst)"
}
# (a) the workspace browser-harness ACTUALLY loads from — ask it via its own python
# (the CLI's shebang points at it); helpers.AGENT_WORKSPACE honours BH_AGENT_WORKSPACE and
# resolves the right default on every version. Fall back to known defaults if the query fails.
WS_NEW=""
BHPY="$(head -1 "$(command -v browser-harness)" 2>/dev/null | sed 's/^#!//;s/ .*//')"
[ -n "$BHPY" ] && [ -x "$BHPY" ] && WS_NEW="$("$BHPY" -c 'from browser_harness.helpers import AGENT_WORKSPACE; print(AGENT_WORKSPACE)' 2>/dev/null)"
[ -z "$WS_NEW" ] && WS_NEW="${BH_AGENT_WORKSPACE:-$HOME/.config/browser-harness/agent-workspace}"
install_helpers_into "$WS_NEW"
# (b) a source checkout's agent-workspace, if present (editable/dev installs)
BH_DIR="${BROWSER_HARNESS_DIR:-}"
if [ -z "$BH_DIR" ]; then
  for c in "$HOME/Developer/browser-harness" "$HOME/browser-harness"; do
    [ -f "$c/agent-workspace/agent_helpers.py" ] && { BH_DIR="$c"; break; }
  done
fi
[ -n "$BH_DIR" ] && [ "$BH_DIR/agent-workspace" != "$WS_NEW" ] && install_helpers_into "$BH_DIR/agent-workspace"

# Keep the stable symlink that ~/.claude/CLAUDE.md's @-import points at aimed at the current
# (Python-version-specific) packaged SKILL, so the import never rots across reinstalls. We
# only refresh the symlink — registering the block in CLAUDE.md is opt-in (./claude-md.sh apply).
# Under npm we don't reach into ~/.claude from a silent postinstall — the next-steps
# note tells the user how to wire SKILL.md into their CLAUDE.md themselves.
if [ -z "${HORSE_FROM_NPM:-}" ]; then
  "$HERE/claude-md.sh" symlink || echo "  (skipped CLAUDE.md SKILL symlink — see ./claude-md.sh)" >&2
fi

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

if command -v browser-harness >/dev/null 2>&1; then
  export BU_CDP_URL="http://127.0.0.1:$PORT"
  read -r -d '' check <<'PY' || true
import browser_harness.helpers as _h
from browser_harness.helpers import cdp
# our helpers must have loaded AND overridden cdp (so auto-home is active), not just added bh_open
if not hasattr(_h, "bh_open") or getattr(_h.cdp, "__module__", "") != "browser_harness_agent_helpers":
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
  echo "Verifying through browser-harness…"
  verified=""; result=""
  for _ in $(seq 1 20); do   # ~40s — must clear the SW's first 30s keepalive tick
    result="$(printf '%s' "$check" | browser-harness 2>/dev/null | tail -1)"
    [ "$result" = "READY" ] && { verified=1; break; }
    [ "$result" = "NOHELPERS" ] && break   # helpers not loading — no point retrying
    sleep 2
  done
  if [ -n "$verified" ]; then
    echo "✓ verified — bh_open loaded + listTabs() answered over CDP; the extension is live"
  elif [ "$result" = "NOHELPERS" ]; then
    echo "! bh_open did NOT load — browser-harness isn't reading the workspace we appended to." >&2
    echo "  Set BH_AGENT_WORKSPACE to its agent-workspace dir and re-run ./install.sh." >&2
  else
    echo "! couldn't confirm the extension within ~40s — check the browser window."
  fi
fi

echo
echo "Next:"
echo "  • Sign into the apps you want your agents to use — logins persist in $PROFILE"
echo "  • Point CDP clients at it:  export BU_CDP_URL=http://127.0.0.1:$PORT"
echo "  • (Re)launch anytime — agents too — with:  horse-browser"
