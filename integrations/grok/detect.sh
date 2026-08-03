# integrations/grok/detect.sh — sourced by bin/horse-browser (driver mode) when no
# HORSE_SESSION is set. Everything grok-specific about identity lives here; the core
# only speaks HORSE_SESSION / HORSE_LANE / BH_ANCHOR_*.
#
# grok marks its tool processes with GROK_AGENT=1 — verified by running
# `env | grep ^GROK_` through a real grok tool call. That is the ONLY grok variable a
# tool process gets. GROK_SESSION_ID exists, but the hook runner injects it into HOOK
# processes only, not tools, so it is not available to us here.
#
# The session id therefore has to be derived. grok keeps ~/.grok/active_sessions.json:
#
#   [{"session_id": "019fb004-…", "pid": 9573, "cwd": "…", "opened_at": "…"}]
#
# …which maps a LIVE grok process to its session. Walking up to the nearest grok
# ancestor and looking that pid up yields the identity and the anchor pid in one pass —
# the same shape as claude-code/detect.sh's anchor walk.
#
# Fallback when the walk finds no registered pid (headless `grok -p` may not register):
# the workspace root. Coarser than a session id — two grok sessions in one directory
# would share a tab group — but stable, and far better than falling through to whatever
# CLAUDE_CODE_SESSION_ID happens to be inherited from a parent shell.

if [ -z "${HORSE_SESSION:-}" ] && [ -n "${GROK_AGENT:-}" ]; then
  _gsid=""
  _gpid=""

  # 1. A hook process (or anything that exported it) already has the real thing.
  if [ -n "${GROK_SESSION_ID:-}" ]; then
    _gsid="$GROK_SESSION_ID"
  fi

  # 2. Otherwise: nearest grok ancestor, then two lookups keyed on that pid —
  #    our own SessionStart hook's file first (it has the real id even for headless
  #    `grok -p`, which does not register anywhere else), then active_sessions.json.
  if [ -z "$_gsid" ] || [ -z "${BH_ANCHOR_PID:-}" ]; then
    _ap=$$
    for _ in $(seq 1 12); do
      _comm=$(ps -o comm= -p "$_ap" 2>/dev/null) || break
      case "${_comm##*/}" in
        grok|grok-*)
          _gpid="$_ap"
          if [ -z "$_gsid" ]; then
            _gsf="${HORSE_BROWSER_GROK_SESSIONS:-$HOME/.config/horse-browser/grok-sessions}/$_ap"
            [ -r "$_gsf" ] && _gsid=$(tr -d '\n' < "$_gsf" 2>/dev/null || true)
          fi
          [ -n "$_gsid" ] || _gsid=$(python3 - "$HOME/.grok/active_sessions.json" "$_ap" <<'PY' 2>/dev/null || true
import json, sys
try:
    rows = json.load(open(sys.argv[1]))
except Exception:
    rows = []
pid = int(sys.argv[2])
for r in rows if isinstance(rows, list) else []:
    if r.get("pid") == pid and r.get("session_id"):
        print(r["session_id"])
        break
PY
)
          break ;;
      esac
      _pp=$(ps -o ppid= -p "$_ap" 2>/dev/null | tr -d ' ')
      if [ -z "$_pp" ] || [ "$_pp" -le 1 ]; then break; fi
      _ap="$_pp"
    done
  fi

  # 3. Last resort: the workspace. Coarse but stable and, crucially, OURS — never a
  #    Claude session id that leaked in through the environment.
  if [ -z "$_gsid" ]; then
    _gsid="ws$(printf '%s' "${GROK_WORKSPACE_ROOT:-$PWD}" | cksum | cut -d' ' -f1)"
  fi

  HORSE_SESSION="grok-$_gsid"
  if [ -z "${BH_ANCHOR_PID:-}" ] && [ -n "$_gpid" ]; then
    export BH_ANCHOR_PID="$_gpid"
    export BH_ANCHOR_START="$(if [ "$(uname -s)" = "Linux" ]; then echo "linux:$(awk '{print $22}' /proc/"$_gpid"/stat 2>/dev/null)"; else echo "darwin:$(ps -o lstart= -p "$_gpid" 2>/dev/null)"; fi)"
  fi
fi
