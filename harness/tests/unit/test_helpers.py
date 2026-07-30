import json
import os
import tempfile
import time
from unittest.mock import patch

import pytest
from PIL import Image

from horse_harness import helpers


def _run(fake_png, width, height, **kwargs):
    fake = lambda method, **_: {"data": fake_png(width, height)}
    with patch("horse_harness.helpers.cdp", side_effect=fake), tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "shot.png")
        helpers.capture_screenshot(path, **kwargs)
        return Image.open(path).size


def test_max_dim_downsizes_oversized_image(fake_png):
    assert max(_run(fake_png, 4592, 2286, max_dim=1800)) == 1800


def test_max_dim_skips_when_image_already_small(fake_png):
    assert _run(fake_png, 800, 400, max_dim=1800) == (800, 400)


def test_max_dim_default_is_no_resize(fake_png):
    assert _run(fake_png, 4592, 2286) == (4592, 2286)


def test_registrable_host_normalizes_to_domain_tld():
    assert helpers._registrable_host("https://mail.google.com/x?y=1") == "google.com"
    assert helpers._registrable_host("http://www.example.com/") == "example.com"
    assert helpers._registrable_host("https://example.com") == "example.com"
    assert helpers._registrable_host("http://localhost:3000/") == "localhost"


def _reset_skill_state(tmp_path, monkeypatch, session):
    monkeypatch.setattr(helpers, "AGENT_WORKSPACE", tmp_path)
    monkeypatch.setattr(helpers.ipc, "_TMP", tmp_path)
    monkeypatch.setenv("BU_NAME", session)
    helpers._hb_skill_state = None


def test_skill_hint_announces_existing_skill_on_first_visit(tmp_path, monkeypatch):
    _reset_skill_state(tmp_path, monkeypatch, "s1")
    site = tmp_path / "domain-skills" / "example.com"
    site.mkdir(parents=True)
    (site / "scraping.md").write_text("hi")
    first = helpers._hb_skill_hint("https://www.example.com/")
    assert first and "site skill" in first[0] and "scraping.md" in first[0]
    assert helpers._hb_skill_hint("https://example.com/other") == []   # deduped after


def test_skill_hint_nudges_after_third_visit_when_absent(tmp_path, monkeypatch):
    _reset_skill_state(tmp_path, monkeypatch, "s2")
    assert helpers._hb_skill_hint("https://acme.io/a") == []
    assert helpers._hb_skill_hint("https://acme.io/b") == []
    third = helpers._hb_skill_hint("https://www.acme.io/c")
    assert third and "no site skill for acme.io" in third[0]
    assert helpers._hb_skill_hint("https://acme.io/d") == []           # once only


def test_page_info_raises_clear_error_on_js_exception():
    def fake_send(req):
        return {}

    def fake_cdp(method, **kwargs):
        return {
            "result": {
                "type": "object",
                "subtype": "error",
                "description": "ReferenceError: location is not defined",
            },
            "exceptionDetails": {
                "text": "Uncaught",
                "lineNumber": 0,
                "columnNumber": 16,
            },
        }

    with patch("horse_harness.helpers._send", side_effect=fake_send), \
         patch("horse_harness.helpers.cdp", side_effect=fake_cdp):
        with pytest.raises(RuntimeError, match="ReferenceError"):
            helpers.page_info()




# --- wait_for_element ---

def test_wait_for_element_returns_true_when_found_immediately():
    def fake_js(expr, **kwargs):
        return True

    with patch("horse_harness.helpers.js", side_effect=fake_js):
        assert helpers.wait_for_element("#target", timeout=2.0) is True


def test_wait_for_element_returns_false_on_timeout():
    def fake_js(expr, **kwargs):
        return False

    with patch("horse_harness.helpers.js", side_effect=fake_js), \
         patch("horse_harness.helpers.time") as mock_time:
        # simulate time advancing past the deadline immediately
        start = time.time()
        mock_time.time.side_effect = [start, start + 5.0]
        mock_time.sleep = lambda _: None
        assert helpers.wait_for_element("#missing", timeout=1.0) is False


def test_wait_for_element_visible_uses_check_visibility():
    js_exprs = []

    def fake_js(expr, **kwargs):
        js_exprs.append(expr)
        return True

    with patch("horse_harness.helpers.js", side_effect=fake_js):
        helpers.wait_for_element("#btn", visible=True)

    # Prefers checkVisibility (walks ancestor chain) with a computed-style
    # fallback for older Chrome.
    assert any("checkVisibility" in e for e in js_exprs)
    assert any("getComputedStyle" in e for e in js_exprs)
    # must NOT use offsetParent (fails for position:fixed elements)
    assert not any("offsetParent" in e for e in js_exprs)


def test_wait_for_element_non_visible_uses_simple_check():
    js_exprs = []

    def fake_js(expr, **kwargs):
        js_exprs.append(expr)
        return True

    with patch("horse_harness.helpers.js", side_effect=fake_js):
        helpers.wait_for_element("#btn", visible=False)

    assert any("querySelector" in e and "offsetParent" not in e for e in js_exprs)


# --- wait_for_network_idle ---

def test_wait_for_network_idle_returns_true_when_no_events():
    call_count = 0

    def fake_send(req):
        nonlocal call_count
        call_count += 1
        return {"events": []}

    with patch("horse_harness.helpers._send", side_effect=fake_send), \
         patch("horse_harness.helpers.time") as mock_time:
        start = 1000.0
        # first call: not idle yet; second call: idle window elapsed
        mock_time.time.side_effect = [start, start, start, start + 0.6, start + 0.6]
        mock_time.sleep = lambda _: None
        result = helpers.wait_for_network_idle(timeout=5.0, idle_ms=500)

    assert result is True


def test_wait_for_network_idle_waits_for_inflight_request():
    # Verifies inflight tracking: must not return True until loadingFinished,
    # even though >idle_ms elapses between requestWillBeSent and loadingFinished.
    # An event-silence-only implementation would return True at iter2 (wrong).
    events_seq = [
        [{"method": "Network.requestWillBeSent", "params": {"requestId": "req1"}}],
        [],   # >500ms elapsed — old impl returns True here; new must NOT
        [{"method": "Network.loadingFinished",   "params": {"requestId": "req1"}}],
        [],   # idle_ms after loadingFinished → return True
    ]
    idx = 0

    def fake_send(req):
        nonlocal idx
        evs = events_seq[min(idx, len(events_seq) - 1)]
        idx += 1
        return {"events": evs}

    with patch("horse_harness.helpers._send", side_effect=fake_send), \
         patch("horse_harness.helpers.time") as mock_time:
        start = 1000.0
        # inflight non-empty → short-circuit skips time.time() in idle check for iter1/iter2
        mock_time.time.side_effect = [
            start, start,       # deadline + last_activity init
            start + 0.1,        # iter1 while-check
            start + 0.1,        # iter1 rWS last_activity update
                                # iter1 idle-check: inflight non-empty → short-circuit
            start + 0.7,        # iter2 while-check (>500ms since rWS but request still in flight)
                                # iter2 idle-check: inflight non-empty → short-circuit
            start + 0.8,        # iter3 while-check
            start + 0.8,        # iter3 lF last_activity update
            start + 0.8,        # iter3 idle-check: 0ms < 500 → not idle
            start + 1.4,        # iter4 while-check
            start + 1.4,        # iter4 idle-check: 600ms >= 500 → True
        ]
        mock_time.sleep = lambda _: None
        result = helpers.wait_for_network_idle(timeout=5.0, idle_ms=500)

    assert result is True
    assert idx == 4  # did not short-circuit at iter2 despite silence > idle_ms


def test_wait_for_network_idle_returns_false_on_timeout():
    # Continuous rWS keeps inflight non-empty → idle check short-circuits every iteration.
    # time.time() is only called for while-check and rWS last_activity (not idle check).
    def fake_send(req):
        return {"events": [{"method": "Network.requestWillBeSent", "params": {"requestId": "r"}}]}

    with patch("horse_harness.helpers._send", side_effect=fake_send), \
         patch("horse_harness.helpers.time") as mock_time:
        start = 1000.0
        mock_time.time.side_effect = [
            start, start,       # deadline + last_activity init
            start + 0.1,        # iter1 while-check (in deadline)
            start + 0.1,        # iter1 rWS last_activity update
                                # iter1 idle-check: inflight non-empty → short-circuit
            start + 20.0,       # iter2 while-check (past deadline → exit)
        ]
        mock_time.sleep = lambda _: None
        result = helpers.wait_for_network_idle(timeout=10.0, idle_ms=500)

    assert result is False



def test_wait_for_network_idle_filters_events_to_active_session():
    """Background tabs (e.g. a polling page the agent switched away from) keep
    emitting Network events into the daemon's global buffer. The wait must
    filter by session_id of the currently-attached tab — otherwise it would
    see the background tab's traffic and either fail to return idle or wait
    on the wrong tab's requests."""
    active = "session-ACTIVE"
    background = "session-BACKGROUND"

    # First /drain_events/ payload: rWS + lF on the BACKGROUND session that we
    # must ignore, plus zero events on the active session. With filtering, the
    # active session sees no traffic and the idle window can elapse.
    events_seq = [
        [
            {"session_id": background, "method": "Network.requestWillBeSent", "params": {"requestId": "bg1"}},
            {"session_id": background, "method": "Network.loadingFinished",   "params": {"requestId": "bg1"}},
        ],
        [],  # second drain — quiet on both sessions; idle window should fire here
    ]
    drain_idx = 0

    def fake_send(req):
        nonlocal drain_idx
        if req.get("meta") == "session":
            return {"session_id": active}
        if req.get("meta") == "drain_events":
            evs = events_seq[min(drain_idx, len(events_seq) - 1)]
            drain_idx += 1
            return {"events": evs}
        return {}

    with patch("horse_harness.helpers._send", side_effect=fake_send), \
         patch("horse_harness.helpers.time") as mock_time:
        start = 1000.0
        # No inflight on active session → idle check uses time.time().
        mock_time.time.side_effect = [start, start, start, start + 0.6, start + 0.6]
        mock_time.sleep = lambda _: None
        result = helpers.wait_for_network_idle(timeout=5.0, idle_ms=500)

    assert result is True, (
        "wait_for_network_idle must return True even when the BACKGROUND "
        "session is busy, as long as the ACTIVE session is idle. Without the "
        "session filter, the background rWS/lF pair would have updated "
        "last_activity and prevented the idle window from elapsing."
    )


# ── attached mode: list_tabs() on a browser with no extension ───────────────────────
# Driving a CDP endpoint we didn't launch means no extension, so no tab groups, so
# listTabs has nothing to read. The fallback is the registry helpers._hb_track keeps.


def _tabs_env(tmp_path, monkeypatch, tracked):
    f = tmp_path / "tabs"
    f.write_text(json.dumps(tracked))
    monkeypatch.setattr(helpers, "_hb_tabs_file", lambda: str(f))
    monkeypatch.setenv("HORSE_SESSION", "hb-test-session-0001")
    return f


def _page_targets(*ids):
    return {"targetInfos": [{"targetId": i, "type": "page",
                             "url": f"https://example.test/{i}", "title": i} for i in ids]}


def test_list_tabs_ignores_the_extension_even_when_it_would_answer(tmp_path, monkeypatch):
    # THE invariant. An extension that answers is still not consulted: two sources would
    # be two answers, and every way they can disagree is a bug — a groupTab that quietly
    # failed, a tab the operator dragged out of the group, a browser with no extension.
    _tabs_env(tmp_path, monkeypatch, ["a"])
    ext = patch("horse_harness.helpers.ext_call", return_value=[{"targetId": "from-ext"}])
    with ext as ext_call, patch("horse_harness.helpers.cdp", return_value=_page_targets("a")):
        got = helpers.list_tabs()
    assert [t["targetId"] for t in got] == ["a"]
    assert ext_call.call_count == 0, "list_tabs must not ask the extension at all"


def test_list_tabs_is_empty_when_the_registry_is(tmp_path, monkeypatch):
    # …and the converse: an empty registry means no tabs, even if the extension would
    # have named some. Nothing resurrects ids the session already dropped.
    _tabs_env(tmp_path, monkeypatch, [])
    with patch("horse_harness.helpers.ext_call", return_value=[{"targetId": "ghost"}]):
        assert helpers.list_tabs() == []


def test_list_tabs_falls_back_to_the_registry_with_no_extension(tmp_path, monkeypatch):
    _tabs_env(tmp_path, monkeypatch, ["a", "b"])
    with patch("horse_harness.helpers.ext_call", return_value=None), \
         patch("horse_harness.helpers.cdp", return_value=_page_targets("a", "b")):
        got = helpers.list_tabs()
    assert [t["targetId"] for t in got] == ["a", "b"]
    assert [t["lastAccessed"] for t in got] == [1, 2]   # tracking order stands in for recency


def test_list_tabs_fallback_forgets_tabs_that_have_closed(tmp_path, monkeypatch):
    f = _tabs_env(tmp_path, monkeypatch, ["a", "gone", "b"])
    with patch("horse_harness.helpers.ext_call", return_value=None), \
         patch("horse_harness.helpers.cdp", return_value=_page_targets("a", "b")):
        got = helpers.list_tabs()
    assert [t["targetId"] for t in got] == ["a", "b"]
    assert json.loads(f.read_text()) == ["a", "b"]      # pruned, not left to grow


def test_hb_track_moves_a_retouched_tab_to_the_end(tmp_path, monkeypatch):
    f = _tabs_env(tmp_path, monkeypatch, ["a", "b", "c"])
    helpers._hb_track("a")
    assert json.loads(f.read_text()) == ["b", "c", "a"]
