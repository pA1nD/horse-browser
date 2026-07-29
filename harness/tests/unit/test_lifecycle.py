"""ensure_daemon's reuse rules — in particular: never reuse, and never end up on, a daemon
pinned to a DIFFERENT browser. Several horse-browsers run side by side (one per agent, each
on its own port + profile) and a daemon's CDP endpoint is frozen at spawn, so reusing one by
name alone can silently drive the wrong browser."""
import pytest
from unittest.mock import patch

from horse_harness import lifecycle


class _FakeProc:
    def poll(self): return None


def _run(monkeypatch, *, want, have, after_restart=None, cdp_ok=True):
    """Call ensure_daemon against a live daemon reporting endpoint `have`, with the caller
    asking for `want`. `after_restart` is what a freshly spawned daemon reports (defaults to
    `want` — the normal case). Returns (restarted, spawned)."""
    calls = {"restart": 0, "spawn": 0}
    state = {"endpoint": have}

    def _restart(name=None):
        calls["restart"] += 1
        state["endpoint"] = want if after_restart is None else after_restart

    monkeypatch.setattr(lifecycle, "daemon_alive", lambda name=None: True)
    monkeypatch.setattr(lifecycle.ipc, "endpoint", lambda name, timeout=1.0: state["endpoint"])
    monkeypatch.setattr(lifecycle.ipc, "connect", lambda name, timeout=3.0: (object(), "tok"))
    monkeypatch.setattr(lifecycle.ipc, "request",
                        lambda c, t, m: {"result": {}} if cdp_ok else {"error": "dead"})
    monkeypatch.setattr(lifecycle, "restart_daemon", _restart)

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
    """Same session name, second browser: the live daemon still holds a socket into the FIRST
    browser, so reuse would drive that one. Restart + respawn on ours."""
    restarted, spawned = _run(monkeypatch, want="http://127.0.0.1:9224", have="http://127.0.0.1:9223")

    assert (restarted, spawned) == (1, 1)


def test_keeps_a_daemon_whose_endpoint_is_unknown(monkeypatch):
    """None is UNKNOWN (daemon too old to report it), not a mismatch — treating it as one
    would restart every healthy daemon across an upgrade."""
    restarted, spawned = _run(monkeypatch, want="http://127.0.0.1:9223", have=None)

    assert (restarted, spawned) == (0, 0)


def test_still_heals_a_stale_daemon_on_the_right_endpoint(monkeypatch):
    """The endpoint check must not shadow the original self-heal: socket alive but the CDP
    websocket dead still means restart."""
    restarted, spawned = _run(monkeypatch, want="http://127.0.0.1:9223",
                              have="http://127.0.0.1:9223", cdp_ok=False)

    assert (restarted, spawned) == (1, 1)


def test_raises_when_another_caller_won_the_spawn_race(monkeypatch):
    """Two concurrent callers, same session name, two browsers: the singleton lock lets one
    spawn win, and the loser's wait loop would otherwise see "alive" and hand back a daemon
    driving the WRONG browser. It must say so instead."""
    with pytest.raises(RuntimeError, match="HORSE_SESSION"):
        _run(monkeypatch, want="http://127.0.0.1:9224", have="http://127.0.0.1:9223",
             after_restart="http://127.0.0.1:9223")


# --- _endpoint_key(): spellings that name the SAME browser ---

@pytest.mark.parametrize("other", [
    "http://127.0.0.1:9223/",
    "http://localhost:9223",
    "ws://127.0.0.1:9223/devtools/browser/6f0a-4b21",
])
def test_equivalent_endpoint_spellings_do_not_restart_a_healthy_daemon(monkeypatch, other):
    """A client naming its browser as a resolved ws:// url (or via localhost, or with a
    trailing slash) must not look like a different browser — that would restart a healthy
    daemon on every call, interrupting whatever it is driving."""
    assert lifecycle._endpoint_key(other) == lifecycle._endpoint_key("http://127.0.0.1:9223")

    restarted, spawned = _run(monkeypatch, want=other, have="http://127.0.0.1:9223")
    assert (restarted, spawned) == (0, 0)


def test_endpoint_key_still_separates_different_ports():
    a, b = lifecycle._endpoint_key("http://127.0.0.1:9223"), lifecycle._endpoint_key("http://127.0.0.1:9224")

    assert a != b and a is not None


def test_endpoint_key_of_nothing_is_none():
    """No endpoint means UNKNOWN, which callers must not read as a mismatch."""
    for v in (None, ""):
        assert lifecycle._endpoint_key(v) is None, f"accepted {v!r}"
