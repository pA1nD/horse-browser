"""drag() must take a hand's time on a machine that is not idle.

The gesture is calibrated against 14 recorded human drags — easing, overshoot, sample spacing,
and DURATION. Duration was the one that only held on an idle machine: the loop dispatched a
sample and then slept the gap, so the real gap was `dispatch + sleep`, and a dispatch is an IPC
round trip whose cost belongs to the machine's load rather than to the gesture. Measured at
~2ms idle and ~66ms under fleet load, which stretched a 1.8s drag to 8.6-9.4s — same commit,
same machine, load the only difference. A nine-second drag of 280px is not a hand, and the
sites this exists to get past score exactly that.

Load is not reproducible in a test, so the cost is injected instead: _cdp_nowait is replaced
with a stub that sleeps a fixed amount. That is the mechanism the real slowdown acts through,
so pinning it here pins the behaviour without needing a busy machine.

Everything is driven through the module's own seams — no browser, no daemon.
"""
import time

import pytest

from horse_harness import helpers


SPAN = 280          # the distance the human traces were recorded at
HUMAN_MAX = 3.0     # recorded hands: ~1.75s median. Past 3s it is not a hand's drag.


class Trace:
    """What the page would have seen: every event, when it arrived."""
    def __init__(self):
        self.events = []          # (t, type, x, y)

    @property
    def moves(self):
        return [e for e in self.events if e[1] == "mouseMoved"]

    def _stamp(self, kind):
        return next((e[0] for e in self.events if e[1] == kind), None)

    @property
    def held(self):
        """Press to release — the span the 14 recorded human drags actually measure, and the
        one drag-profile asserts. The reach beforehand is real motion but not part of it."""
        return self._stamp("mouseReleased") - self._stamp("mousePressed")

    @property
    def total(self):
        return self.events[-1][0] - self.events[0][0]


@pytest.fixture
def drag_probe(monkeypatch):
    """Run drag() against stubs, with a chosen per-dispatch cost. Returns a Trace."""
    def run(dispatch_cost):
        tr = Trace()

        def slow_dispatch(method, **params):
            if dispatch_cost:
                time.sleep(dispatch_cost)
            tr.events.append((time.monotonic(), params.get("type"),
                              params.get("x"), params.get("y")))

        monkeypatch.setattr(helpers, "_cdp_nowait", slow_dispatch)
        monkeypatch.setattr(helpers, "cdp", lambda method, **p: slow_dispatch(method, **p) or {})
        monkeypatch.setattr(helpers, "_pt", lambda target: target)
        monkeypatch.setattr(helpers, "_mouse", {"x": 20.0, "y": 20.0})

        helpers.drag((100.0, 220.0), to=(100.0 + SPAN, 220.0))
        return tr

    return run


def test_idle_machine_drag_is_hand_shaped(drag_probe):
    """The calibrated case still holds: free dispatch, a hand's duration and sample count."""
    tr = drag_probe(0.0)
    assert 0.5 < tr.held < HUMAN_MAX, f"press->release took {tr.held:.2f}s on an idle machine"
    assert len(tr.moves) >= 40, f"only {len(tr.moves)} samples — a hand emits ~63 over this span"


def test_slow_dispatch_costs_samples_not_seconds(drag_probe):
    """THE regression. 30ms per dispatch is what a loaded machine actually did to us.

    Before the fix this ran ~4x over budget, because every sample paid dispatch AND the full
    planned gap. The drag must now thin out instead: still a hand's duration, fewer samples.
    """
    tr = drag_probe(0.030)
    assert tr.held < HUMAN_MAX, (
        f"press->release took {tr.held:.2f}s with 30ms dispatches — "
        f"load must cost samples, not seconds")
    assert len(tr.moves) >= 18, f"thinned to {len(tr.moves)} samples — too sparse to read as a trace"


def test_the_reach_before_the_press_is_paced_too(drag_probe):
    """The approach and the hover over the control are motion the page sees, and they were
    fixed sample counts — so on a slow machine they crawled while the drag body behaved.
    Bounding only the part under test is how you ship a gesture that is still wrong."""
    tr = drag_probe(0.030)
    assert tr.total < 4.0, f"whole gesture took {tr.total:.2f}s — the reach is not paced"


def test_the_path_still_completes_under_load(drag_probe):
    """Thinning must not abandon the gesture partway: it still has to arrive on the target."""
    tr = drag_probe(0.030)
    end_x = tr.moves[-1][2]
    assert abs(end_x - (100.0 + SPAN)) < 1.0, f"released at x={end_x}, target was {100.0 + SPAN}"


def test_samples_are_never_bunched_to_catch_up(drag_probe):
    """Falling behind must not produce a burst of zero-gap samples.

    Back-to-back identical timestamps are their own tell, and 'catch up by firing everything
    at once' is the obvious wrong way to hold a duration target.
    """
    tr = drag_probe(0.020)
    ts = [e[0] for e in tr.moves]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    assert min(gaps) >= 0.015, f"smallest gap {min(gaps)*1000:.1f}ms — samples bunched up"
