#!/usr/bin/env python3
"""Wire — or repair — the Claude Code subagent lane hook in a settings.json.

    lane_hook_wire.py <path-to-lane-hook.sh> <path-to-settings.json>

THE single implementation. bin/horse-browser calls it at launch (throttled) and
install.sh calls it at install time; it used to be inline python in install.sh only,
which is how the hook came to be missing for two days:

  • install.sh skips the wiring entirely under HORSE_FROM_NPM, because a silent
    postinstall must not edit ~/.claude — so `npm i -g`, the documented install,
    never wired it at all.
  • nothing re-checked afterwards, so anything that removed the entry (a demo run
    that stripped it, a hand-edit) was permanent and silent.
  • the command is stored as an ABSOLUTE path. Reinstall somewhere else — npm to a
    dev symlink, a moved checkout — and the entry points at a file that no longer
    exists, and every Bash call in every Claude session runs a hook that isn't there.

Hence "repair", not just "wire": a lane-hook.sh entry whose file is gone gets pointed
at this one.

Quiet when already correct, one stderr line when it changes something. Always exits 0
— a hook we could not wire must never stop the browser from starting.
"""
import json
import os
import sys

PRE_EVENT = "PreToolUse"
STOP_EVENT = "SubagentStop"


def _hook_entries(hooks, event):
    for entry in hooks.get(event, []) or []:
        for h in entry.get("hooks", []) or []:
            yield h


def wire(hook, path):
    """Returns a list of changes made ([] = already correct), or None if we bailed."""
    data = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f) or {}
        except (OSError, ValueError):
            return None          # unreadable or corrupt — never overwrite the operator's file
    if not isinstance(data, dict):
        return None
    hooks = data.setdefault("hooks", {})
    changed = []

    # Repair before adding, or a stale entry survives next to a fresh one and still
    # fires (and fails) on every Bash call.
    for event in (PRE_EVENT, STOP_EVENT):
        for h in _hook_entries(hooks, event):
            cmd = h.get("command") or ""
            if cmd.endswith("lane-hook.sh") and cmd != hook and not os.path.exists(cmd):
                h["command"] = hook
                changed.append("repaired a stale %s path" % event)

    def already(event):
        return any((h.get("command") or "") == hook for h in _hook_entries(hooks, event))

    if not already(PRE_EVENT):
        hooks.setdefault(PRE_EVENT, []).append({
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": hook, "timeout": 10}],
        })
        changed.append("wired %s" % PRE_EVENT)
    if not already(STOP_EVENT):
        hooks.setdefault(STOP_EVENT, []).append({
            "hooks": [{"type": "command", "command": hook, "timeout": 30, "async": True}],
        })
        changed.append("wired %s" % STOP_EVENT)

    if not changed:
        return []
    # Atomic: settings.json is the operator's file and holds far more than our hook.
    # A crash between truncate and write would take all of it.
    tmp = path + ".hb-tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    return changed


def main():
    if len(sys.argv) != 3:
        return 0
    hook, path = sys.argv[1], sys.argv[2]
    try:
        changed = wire(hook, path)
    except OSError as e:
        print("horse-browser: could not wire the subagent lane hook (%s) — subagents "
              "will share one browser lane" % e, file=sys.stderr)
        return 0
    if changed is None:
        print("horse-browser: %s is unreadable — left it alone. The subagent lane hook "
              "is not wired, so subagents share one browser lane." % path, file=sys.stderr)
    elif changed:
        print("horse-browser: subagent lane hook — %s (new Claude Code sessions pick it up)"
              % "; ".join(changed), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
