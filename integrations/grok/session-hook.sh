#!/bin/sh
# integrations/grok/session-hook.sh — grok SessionStart/SessionEnd hook.
#
# Publishes the real grok session id where a TOOL process can find it.
#
# Why this is needed at all: grok injects GROK_SESSION_ID into HOOK processes only. A tool
# process — which is what runs `horse-browser` — gets exactly one grok variable, GROK_AGENT=1
# (verified by running `env` through a real grok tool call). Without help, detect.sh can only
# fall back to hashing the workspace root, so two grok sessions in one directory collide.
#
# The key is the grok PROCESS id, because that is the one thing a hook and a tool process can
# both compute and agree on: both walk up their own ancestry to the nearest grok. Verified:
# every tool call in a grok session, parent agent and subagents alike, descends from the same
# grok pid.
#
# That last fact is also why this does NOT do lanes. grok's subagents share the parent's
# process AND its tool environment, and grok's PreToolUse can only allow/deny — it cannot
# rewrite the command the way Claude Code's can. So a subagent's tool call is
# indistinguishable from the parent's at the point where we would have to act. See
# docs/grok-integration.md.
#
# Registered by bin/horse-browser (ensure_lane_hook) into ~/.grok/hooks/horse-browser.json.
# Always exits 0 — a hook of ours must never interfere with the operator's session.

DIR="${HORSE_BROWSER_GROK_SESSIONS:-$HOME/.config/horse-browser/grok-sessions}"

# The nearest grok ancestor of THIS process. Same walk as integrations/grok/detect.sh;
# they must agree or the file is written under a key nobody reads.
grok_pid() {
  _p=$$
  _n=0
  while [ "$_n" -lt 12 ]; do
    _comm=$(ps -o comm= -p "$_p" 2>/dev/null) || return 1
    case "${_comm##*/}" in
      grok|grok-*) printf '%s' "$_p"; return 0 ;;
    esac
    _pp=$(ps -o ppid= -p "$_p" 2>/dev/null | tr -d ' ')
    [ -n "$_pp" ] && [ "$_pp" -gt 1 ] || return 1
    _p="$_pp"
    _n=$((_n + 1))
  done
  return 1
}

# HORSE_BROWSER_GROK_PID: test seam only. There is no grok ancestor under a test runner,
# so the walk would bail and the hook could never be exercised.
gp="${HORSE_BROWSER_GROK_PID:-$(grok_pid || true)}"
[ -n "$gp" ] || exit 0

case "${GROK_HOOK_EVENT:-}" in
  session_end)
    rm -f "$DIR/$gp" 2>/dev/null
    ;;
  *)
    # session_start, or anything else we get wired to: (re)assert it. Cheap and idempotent.
    [ -n "${GROK_SESSION_ID:-}" ] || exit 0
    mkdir -p "$DIR" 2>/dev/null || exit 0
    printf '%s\n' "$GROK_SESSION_ID" > "$DIR/$gp" 2>/dev/null
    ;;
esac
exit 0
