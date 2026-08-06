#!/usr/bin/env python3
"""Reconcile every piece of state horse-browser owns OUTSIDE its own directories.

    reconcile_external.py            manifest on stdin, one line per change on stderr
    reconcile_external.py --print-rule <root>     the rule file's exact content, to stdout

Three targets, one mechanism:

    ~/.claude/settings.json           2 hook entries      merge (shared file)
    ~/.grok/hooks/horse-browser.json  the whole file      overwrite
    ~/.claude/rules/horse-browser.md  the whole file      overwrite

They used to have three separate implementations and three trigger policies; the odd one out
(the Claude hooks) grew a bug the other two could not have, because it is the only target
whose file it does not own. It APPENDED, so every checkout that ever ran the launcher left a
permanent entry: ten registrations were found on the operator's machine where one belongs, and
a temp checkout that had since been deleted made every Bash call in every session fail a hook.

So the rule here is LAST-WRITER-WINS, not repair-if-broken. We do not ask whether the entry on
disk is stale, whether its file still exists, or whether we are already present — we make our
entries be exactly the entries that are there, every time. Idempotence falls out for free, and
the only way to accumulate is to run two installs, which now converge instead of piling up.

Whether to run at all is decided by the caller (bin/horse-browser gates on a cheap fingerprint,
and refuses outright from a temp tree). This script always does the full comparison, and writes
only what actually differs — so a no-op run is silent and touches nothing.

Manifest (stdin):

    {"root": "<package dir>",
     "targets": {"claude_hooks": {"path": "…/settings.json", "enabled": true},
                 "grok_hook":    {"path": "…/horse-browser.json", "enabled": true},
                 "rule":         {"path": "…/horse-browser.md", "enabled": false}}}

A key that is absent is not applicable on this machine (no ~/.grok, say) and is left alone.
enabled:false means the operator turned it off — we REMOVE our file, because a stale copy of
instructions for a tool you have disabled is worse than none.
"""
import json
import os
import shlex
import sys

PRE_EVENT = "PreToolUse"
STOP_EVENT = "SubagentStop"

RULE_HEADER = (
    "<!-- horse-browser rule — managed by @pa1nd/horse-browser: this whole file is\n"
    "     overwritten, and kept current as the package updates. Edit RULE.md in the\n"
    "     package to change it, or run `horse-browser rule off` to remove it. -->\n"
)


# ── what each target should contain ───────────────────────────────────────────────────

def claude_hook_path(root):
    return os.path.join(root, "integrations", "claude-code", "lane-hook.sh")


def guarded(script):
    """The command we register, wrapped so a MISSING script is a silent no-op.

    npm fires no uninstall hook — verified: neither preuninstall nor postuninstall runs for
    `npm rm -g` on npm 10+. So an uninstalled package leaves its entry behind in a config file
    we can no longer reach, and a hook whose file is gone fails on EVERY tool call, in every
    session, for as long as the entry survives. That is not hypothetical: it is the bug that
    started all of this, and back then the file had merely moved.

    `[ -x … ] || exit 0` costs nothing (the shell would spawn to run the script anyway, and this
    execs into it) and turns "package removed" from a machine-wide breakage into nothing at all.
    """
    q = shlex.quote(script)
    return "sh -c %s" % shlex.quote("[ -x %s ] || exit 0; exec %s" % (q, q))


def grok_hook_want(root):
    script = os.path.join(root, "integrations", "grok", "session-hook.sh")
    if not os.path.exists(script):
        return None
    return json.dumps({"hooks": {ev: [{"hooks": [
        {"type": "command", "command": guarded(script), "timeout": 10}]}]
        for ev in ("SessionStart", "SessionEnd")}}, indent=2) + "\n"


def rule_want(root):
    src = os.path.join(root, "RULE.md")
    try:
        with open(src) as f:
            return RULE_HEADER + f.read()
    except OSError:
        return None


# ── the shared file: ~/.claude/settings.json ──────────────────────────────────────────

def _is_ours(hook):
    """Substring, not endswith: the registered command wraps the path (see `guarded`), and an
    entry written by an older version ends with the bare path. Both are ours to replace."""
    return "lane-hook.sh" in (hook.get("command") or "")


def _canonical(event, hook):
    """The one block we want for an event. Rebuilt from scratch every time rather than
    patched in place, so a hand-edited timeout or a missing `async` self-heals too."""
    cmd = guarded(hook)
    if event == PRE_EVENT:
        return {"matcher": "Bash",
                "hooks": [{"type": "command", "command": cmd, "timeout": 10}]}
    return {"hooks": [{"type": "command", "command": cmd, "timeout": 30, "async": True}]}


def _without_ours(blocks):
    """Every block minus our hooks. A block that existed only for us disappears; a block we
    share with another tool keeps that tool's hooks — rewriting someone else's broken hook to
    point at ours would be a far worse bug than the one this fixes."""
    out = []
    for blk in blocks or []:
        hooks = blk.get("hooks") or []
        kept = [h for h in hooks if not _is_ours(h)]
        if not kept:
            continue
        out.append(blk if len(kept) == len(hooks) else {**blk, "hooks": kept})
    return out


def reconcile_claude_hooks(path, hook):
    """Returns (changes, None) or ([], reason-we-bailed)."""
    data = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f) or {}
        except (OSError, ValueError):
            return [], "%s is unreadable — left it alone" % path
    if not isinstance(data, dict):
        return [], "%s is not a JSON object — left it alone" % path

    hooks = data.setdefault("hooks", {})
    changes = []
    for event in (PRE_EVENT, STOP_EVENT):
        before = hooks.get(event) or []
        after = _without_ours(before) + [_canonical(event, hook)]
        if after == before:
            continue
        had = sum(1 for blk in before for h in blk.get("hooks") or [] if _is_ours(h))
        hooks[event] = after
        if had == 0:
            changes.append("wired %s" % event)
        elif had > 1:
            changes.append("collapsed %d %s entries to 1" % (had, event))
        else:
            changes.append("repointed %s" % event)

    if changes:
        _write_atomic(path, json.dumps(data, indent=2) + "\n")
    return changes, None


# ── whole files we own outright ───────────────────────────────────────────────────────

def reconcile_file(path, want):
    """Overwrite on drift, silent when identical. want=None removes the file."""
    try:
        with open(path) as f:
            have = f.read()
    except OSError:
        have = None

    if want is None:
        if have is None:
            return []
        os.remove(path)
        return ["removed %s" % path]

    if have == want:
        return []
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    _write_atomic(path, want)
    return ["%s %s" % ("wrote" if have is None else "refreshed", path)]


def _write_atomic(path, text):
    """These are the operator's files and hold far more than ours. A crash between truncate
    and write would take all of it."""
    tmp = path + ".hb-tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# ── driver ────────────────────────────────────────────────────────────────────────────

def reconcile(manifest):
    root = manifest["root"]
    targets = manifest.get("targets") or {}
    changes, problems = [], []

    spec = targets.get("claude_hooks")
    if spec and spec.get("enabled"):
        hook = claude_hook_path(root)
        if os.path.exists(hook):
            got, why = reconcile_claude_hooks(spec["path"], hook)
            changes += got
            if why:
                problems.append(why)

    for key, want_fn in (("grok_hook", grok_hook_want), ("rule", rule_want)):
        spec = targets.get(key)
        if not spec:
            continue
        want = want_fn(root) if spec.get("enabled") else None
        # A source we cannot read is a broken package, not a request to delete the target.
        if spec.get("enabled") and want is None:
            problems.append("%s source missing under %s — left %s alone" % (key, root, spec["path"]))
            continue
        try:
            changes += reconcile_file(spec["path"], want)
        except OSError as e:
            problems.append("could not write %s (%s)" % (spec["path"], e))

    return changes, problems


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--print-rule":
        want = rule_want(sys.argv[2])
        if want is None:
            print("horse-browser: RULE.md missing under %s" % sys.argv[2], file=sys.stderr)
            return 1
        sys.stdout.write(want)
        return 0

    try:
        manifest = json.load(sys.stdin)
    except ValueError as e:
        print("horse-browser: bad reconcile manifest (%s)" % e, file=sys.stderr)
        return 0

    try:
        changes, problems = reconcile(manifest)
    except OSError as e:
        # Never stop the browser from starting over a config file.
        print("horse-browser: could not reconcile external state (%s)" % e, file=sys.stderr)
        return 0

    for p in problems:
        print("horse-browser: %s" % p, file=sys.stderr)
    if changes:
        print("horse-browser: %s" % "; ".join(changes), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
