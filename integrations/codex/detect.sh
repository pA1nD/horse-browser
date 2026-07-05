# integrations/codex/detect.sh — sourced by bin/horse-browser (driver mode) when no
# HORSE_SESSION is set. Codex CLI exports CODEX_THREAD_ID — a per-session UUID — into
# every shell command it runs (verified on codex-cli 0.142.5), so each Codex session
# gets its own tab group + daemon exactly like a Claude session does.
#
# NB: under Codex's default seatbelt sandbox, shell commands get no network (not even
# 127.0.0.1) and no `ps`, so the browser is only drivable from a session that allows
# it (e.g. `codex --sandbox danger-full-access`, or workspace-write with network
# enabled). Detection itself is harmless either way.
#
# Nested-agent note: detect scripts run in integrations/* glob order (claude-code
# first). A Codex session launched FROM a Claude session inherits the Claude vars, so
# the claude-code adapter claims it — acceptable: that Codex run is acting for the
# Claude session, and under its default sandbox it can't reach the browser anyway.

if [ -n "${CODEX_THREAD_ID:-}" ]; then
  HORSE_SESSION="$CODEX_THREAD_ID"

  # Anchor the daemon to the codex process so reap_orphan_daemons can clean it up
  # once the session exits. Under seatbelt `ps` is denied — the walk quietly yields
  # no anchor, and the (conservative) reaper simply skips those daemons.
  if [ -z "${BH_ANCHOR_PID:-}" ]; then
    _ap=$$
    for _ in $(seq 1 12); do
      _comm=$(ps -o comm= -p "$_ap" 2>/dev/null) || break
      case "$_comm" in
        *codex*) export BH_ANCHOR_PID="$_ap"
                 export BH_ANCHOR_START="$(ps -o lstart= -p "$_ap" 2>/dev/null)"; break ;;
      esac
      _pp=$(ps -o ppid= -p "$_ap" 2>/dev/null | tr -d ' ')
      if [ -z "$_pp" ] || [ "$_pp" -le 1 ]; then break; fi
      _ap="$_pp"
    done
  fi
fi
