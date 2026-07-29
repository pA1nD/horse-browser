"""ensure_daemon's reuse rules — in particular: never reuse a daemon that is
pinned to a DIFFERENT browser. Several horse-browsers run side by side (one per
agent, each on its own port + profile) and a daemon's CDP endpoint is frozen at
spawn, so reusing one by name alone can silently drive the wrong browser."""
from unittest.mock import patch

from horse_harness import lifecycle


class _FakeProc:
    def poll(self): return None


def _run(monkeypatch, *, want, have, cdp_ok=True):
    """Call ensure_daemon against a live daemon reporting endpoint `have`, with the
    caller asking for `want`. Returns (restarted, spawned)."""
    calls = {"restart": 0, "spawn": 0}
    monkeypatch.setattr(lifecycle, "daemon_alive", lambda name=None: True)
    monkeypatch.setattr(lifecycle.ipc, "endpoint", lambda name, timeout=1.0: have)
    monkeypatch.setattr(lifecycle.ipc, "connect", lambda name, timeout=3.0: (object(), "tok"))
    monkeypatch.setattr(lifecycle.ipc, "request",
                        lambda c, t, m: {"result": {}} if cdp_ok else {"error": "dead"})
    monkeypatch.setattr(lifecycle, "restart_daemon",
                        lambda name=None: calls.__setitem__("restart", calls["restart"] + 1))

    def _popen(*a, **kw):
        calls["spawn"] += 1
        return _FakeProc()

    with patch.object(lifecycle.subprocess, "Popen", _popen):
        lifecycle.ensure_daemon(wait=1.0, name="hb-test", env={"BU_CDP_URL": want})
    return calls["restart"], calls["spawn"]


def test_reuses_a_daemon_on_the_same_endpoint(monkeypatch):
    restarted, spawned = _run(monkeypatch, want="http://127.0.0.1:9223", have="http://127.0.0.1:9223")

    assert (restarted, spawned) == (0, 0)


def test_rebuilds_a_daemon_pinned_to_another_browser(monkeypatch):
    """Same session name, second browser: the live daemon still holds a socket into
    the FIRST browser, so reuse would drive that one. Restart + respawn on ours."""
    restarted, spawned = _run(monkeypatch, want="http://127.0.0.1:9224", have="http://127.0.0.1:9223")

    assert (restarted, spawned) == (1, 1)


def test_keeps_a_daemon_whose_endpoint_is_unknown(monkeypatch):
    """None is UNKNOWN (daemon too old to report it), not a mismatch — treating it
    as one would restart every healthy daemon across an upgrade."""
    restarted, spawned = _run(monkeypatch, want="http://127.0.0.1:9223", have=None)

    assert (restarted, spawned) == (0, 0)


def test_still_heals_a_stale_daemon_on_the_right_endpoint(monkeypatch):
    """The endpoint check must not shadow the original self-heal: socket alive but
    the CDP websocket dead still means restart."""
    restarted, spawned = _run(monkeypatch, want="http://127.0.0.1:9223",
                              have="http://127.0.0.1:9223", cdp_ok=False)

    assert (restarted, spawned) == (1, 1)
