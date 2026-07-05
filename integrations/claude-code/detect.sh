# integrations/claude-code/detect.sh — sourced by bin/horse-browser (driver mode)
# when no HORSE_SESSION is set. Everything Claude-Code-specific about identity
# lives here; the core only speaks HORSE_SESSION / HORSE_LANE / BH_ANCHOR_*.
#
# Contract for a detect.sh: set HORSE_SESSION (any stable per-agent-session string)
# if — and only if — your agent system is detected; optionally set BH_ANCHOR_PID /
# BH_ANCHOR_START to the process the session's daemon should die with. Must be
# side-effect-free when your system isn't present. Sourced under `set -euo pipefail`.

if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
  HORSE_SESSION="$CLAUDE_CODE_SESSION_ID"

  # Anchor the daemon to this Claude session's process: a self-reaping browser-harness
  # exits with the session, and reap_orphan_daemons can clean it up on a stock one. Walk
  # up to the nearest `claude` ancestor; if not found, leave it unset (reaper skips it).
  if [ -z "${BH_ANCHOR_PID:-}" ]; then
    _ap=$$
    for _ in $(seq 1 12); do
      _comm=$(ps -o comm= -p "$_ap" 2>/dev/null) || break
      case "$_comm" in
        *claude*) export BH_ANCHOR_PID="$_ap"
                  export BH_ANCHOR_START="$(ps -o lstart= -p "$_ap" 2>/dev/null)"; break ;;
      esac
      _pp=$(ps -o ppid= -p "$_ap" 2>/dev/null | tr -d ' ')
      if [ -z "$_pp" ] || [ "$_pp" -le 1 ]; then break; fi
      _ap="$_pp"
    done
  fi
fi
