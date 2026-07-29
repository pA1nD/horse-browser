"""Daemon lifecycle: spawn, liveness, restart. Extracted from browser-harness admin.py.

horse-browser always supplies the CDP endpoint (BU_CDP_URL / BU_CDP_WS), so the
local-Chrome discovery and permission-popup recovery flows are gone.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

from . import _ipc as ipc
from . import paths


def _process_start_time(pid):
    """Opaque process-start-time fingerprint at PID, or None if unavailable.

    Two reads returning the same non-None value mean the PID still refers to
    the same process; a different value means the PID was reused. Used by
    restart_daemon() to keep the force-kill recovery path working even when
    the daemon has already torn down its IPC socket, without falling back to
    "trust the pid file" — which would re-introduce the PID-reuse hazard.

    Linux:   /proc/<pid>/stat field 22 (starttime in clock ticks since boot).
    macOS:   `ps -o lstart= -p <pid>` (an absolute timestamp string).
    Anywhere else: returns None; restart_daemon falls back to its strict
    identify-only check, which is safer than no check at all.
    """
    if type(pid) is not int or pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                raw = f.read().decode("ascii", errors="replace")
        except (FileNotFoundError, PermissionError, OSError):
            return None
        # Field 2 is `(comm)`; comm can contain spaces and parens, so split off
        # everything after the LAST `)` and index from there.
        try:
            tail = raw[raw.rindex(")") + 2:].split()
            return tail[19]  # starttime is field 22 (0-indexed: 21 - skipped 2 = 19)
        except (ValueError, IndexError):
            return None
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                stderr=subprocess.DEVNULL, timeout=2,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        s = out.decode("ascii", errors="replace").strip()
        return s or None
    return None


def _load_env():
    repo_root = Path(__file__).resolve().parents[2]
    workspace = paths.workspace_dir()
    for p in (repo_root / ".env", workspace / ".env"):
        if not p.exists():
            continue
        _load_env_file(p)


def _load_env_file(p):
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

NAME = os.environ.get("BU_NAME", "default")


def _log_tail(name):
    try:
        return ipc.log_path(name or NAME).read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1]
    except (FileNotFoundError, IndexError, OSError):
        return None


def daemon_alive(name=None):
    # Ping handshake (not a bare connect) so a stale .port file + port reuse
    # after a daemon crash doesn't make us mistake an unrelated listener for ours.
    return ipc.ping(name or NAME, timeout=1.0)


def ensure_daemon(wait=60.0, name=None, env=None):
    """Idempotent. Self-heals a stale daemon (socket alive, CDP WS dead) — and one
    pinned to a DIFFERENT browser than the caller asked for."""
    e = {**os.environ, **({"BU_NAME": name} if name else {}), **(env or {})}
    if daemon_alive(name):
        # Daemons are reused by NAME, but this machine runs several browsers at once
        # (one per agent, each its own port + profile) and a daemon's endpoint is frozen
        # at spawn. Distinct agents have distinct names, so this only fires when ONE
        # session points at a second browser — where reuse would silently drive the
        # wrong one. Unknown (old daemon, unreadable) is never a mismatch.
        want = e.get("BU_CDP_WS") or e.get("BU_CDP_URL")
        have = ipc.endpoint(name or NAME)
        if want and have and have != want:
            restart_daemon(name)                     # pinned elsewhere — rebuild on ours
        else:
            # Stale daemons accept connects AND reply to meta:* (pure Python) even when the
            # CDP WS to Chrome is dead — probe with a real CDP call and require "result".
            for last in (False, True):
                try:
                    s, token = ipc.connect(name or NAME, timeout=3.0)
                    resp = ipc.request(s, token, {"method": "Target.getTargets", "params": {}})
                    if "result" in resp: return
                except Exception:
                    pass
                if not last: time.sleep(0.5)
            restart_daemon(name)

    try:
        stderr_sink = open(ipc.log_path(name or NAME), "ab")
    except OSError:
        stderr_sink = subprocess.DEVNULL
    p = subprocess.Popen(
        [sys.executable, "-m", "horse_harness.daemon"],
        env=e, stdout=subprocess.DEVNULL, stderr=stderr_sink, **ipc.spawn_kwargs(),
    )
    if stderr_sink is not subprocess.DEVNULL:
        stderr_sink.close()
    deadline = time.time() + wait
    while time.time() < deadline:
        if daemon_alive(name): return
        if p.poll() is not None: break
        time.sleep(0.2)
    msg = _log_tail(name) or ""
    raise RuntimeError(msg or f"daemon {name or NAME} didn't come up -- check {ipc.log_path(name or NAME)}")


def restart_daemon(name=None):
    """Best-effort daemon shutdown + socket/pid cleanup.

    Name is historical: callers typically follow this with another
    invocation, which auto-spawns a fresh daemon via ensure_daemon().
    The function itself only stops.

    Identity is verified via ipc.identify() before any process signal, so
    a stale pid file whose number has been reused by an unrelated process
    is never SIGTERM'd. If the daemon is unreachable, we just clean up the
    pid file and socket and return — never escalate to a kill-by-pid-file.
    """
    import signal

    name = name or NAME
    pid_path = str(ipc.pid_path(name))

    # Two pieces of information are tracked separately:
    #   - daemon_pid: the daemon's self-reported PID, or None.
    #   - daemon_alive: whether ANY daemon answers ping. Keeps the shutdown
    #     IPC path working across upgrades.
    daemon_pid = ipc.identify(name, timeout=5.0)
    alive = daemon_pid is not None or ipc.ping(name, timeout=1.0)
    # Snapshot the daemon's process start-time as a secondary identity check.
    # The IPC socket can disappear before the process exits, so identify()
    # going None partway through is not proof of process death. Comparing
    # start-time before SIGTERM recovers the force-kill behavior for slow
    # shutdowns without re-opening the PID-reuse hole.
    daemon_start = _process_start_time(daemon_pid)

    if alive:
        try:
            c, token = ipc.connect(name, timeout=5.0)
            ipc.request(c, token, {"meta": "shutdown"})
            c.close()
        except Exception:
            pass

    if daemon_pid is not None:
        for _ in range(75):
            try:
                os.kill(daemon_pid, 0)
                time.sleep(0.2)
            except (ProcessLookupError, OSError, SystemError, OverflowError):
                break
        else:
            # Re-verify identity before escalating to SIGTERM. Two acceptable
            # signals, in priority order:
            #   1. ipc.identify() still returns the same PID — daemon's IPC is
            #      live, daemon is wedged. Safe to kill.
            #   2. start-time fingerprint of the original PID is unchanged —
            #      same process, just slow to exit. The IPC may already be
            #      gone; that's expected.
            # If neither holds, the PID may have been reused; skip SIGTERM.
            verified_pid = ipc.identify(name, timeout=1.0)
            same_process = verified_pid == daemon_pid or (
                daemon_start is not None
                and _process_start_time(daemon_pid) == daemon_start
            )
            if same_process:
                try:
                    os.kill(daemon_pid, signal.SIGTERM)
                except (ProcessLookupError, OSError, SystemError, OverflowError):
                    pass

    ipc.cleanup_endpoint(name)
    try:
        os.unlink(pid_path)
    except FileNotFoundError:
        pass
