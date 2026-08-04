"""drag() must take a hand's time on a machine that is not idle.

The gesture is calibrated against 14 recorded human drags — easing, overshoot, sample spacing,
and DURATION. Duration was the one that only held on an idle machine: each loop dispatched a
sample and then slept the planned gap, so the real gap was `dispatch + sleep`, and a dispatch is
an IPC round trip whose cost belongs to the machine's load rather than to the gesture. Measured
at ~2ms idle and ~66ms under fleet load, which stretched a 1.8s drag to 8.6-9.4s — same commit,
same machine, load the only difference. A nine-second drag of 280px is not a hand, and the sites
this exists to get past score exactly that.

Everything here runs on a VIRTUAL clock. The first version of this file injected a real
`time.sleep` per dispatch and asserted real durations, which made it a test that failed when the
machine was busy — the precise defect it exists to catch, reproduced in the test. Simulating the
clock instead makes the injected dispatch cost the only variable, so these assertions mean the
same thing on an idle laptop and under a full fleet run. Real-world timing is covered end to end
by tests/drag-profile.sh, which drives an actual browser.
"""
import pytest

from horse_harness import helpers


SPAN = 280          # the distance the human traces were recorded at
HUMAN_MAX = 3.0     # recorded hands: ~1.75s median. Past 3s it is not a hand's drag.


class Clock:
    """A stand-in for `time` where nothing actually waits.

    drag() reads the clock to decide where the pointer should be, so the clock has to advance
    for the same reasons it would in reality: sleeping advances it, and so does every dispatch,
    by the cost we are simulating.
    """
    def __init__(self, dispatch_cost):
        self.now = 1000.0
        self.cost = dispatch_cost

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, s):
        if s > 0:
            self.now += s

    def spend_dispatch(self):
        self.now += self.cost


class Trace:
    """What the page would have seen: every event, at the virtual time it arrived."""
    def __init__(self):
        self.events = []          # (t, type, x, y)

    @property
    def moves(self):
        return [e for e in self.events if e[1] == "mouseMoved"]

    def _stamp(self, kind):
        return next(e[0] for e in self.events if e[1] == kind)

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
    """Run drag() on a virtual clock with a chosen per-dispatch cost. Returns a Trace."""
    def run(dispatch_cost):
        clock = Clock(dispatch_cost)
        tr = Trace()

        def dispatch(method, **params):
            clock.spend_dispatch()
            tr.events.append((clock.now, params.get("type"),
                              params.get("x"), params.get("y")))

        monkeypatch.setattr(helpers, "_it", clock)
        monkeypatch.setattr(helpers, "_cdp_nowait", dispatch)
        monkeypatch.setattr(helpers, "cdp", lambda method, **p: dispatch(method, **p) or {})
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


def test_load_is_paid_in_samples(drag_probe):
    """The trade itself: a slower machine buys the same duration with fewer samples."""
    fast, slow = drag_probe(0.0), drag_probe(0.030)
    assert len(slow.moves) < len(fast.moves), (
        f"{len(slow.moves)} samples under load vs {len(fast.moves)} idle — "
        f"nothing was traded, so the time went somewhere else")


def test_the_reach_before_the_press_is_paced_too(drag_probe):
    """The approach and the hover over the control are motion the page sees, and they were
    fixed sample counts — so on a slow machine they crawled while the drag body behaved.
    Bounding only the part under test is how you ship a gesture that is still wrong."""
    tr = drag_probe(0.030)
    assert tr.total < 4.0, f"whole gesture took {tr.total:.2f}s — the reach is not paced"


def test_shape_wins_over_the_stopwatch_when_dispatch_is_hopeless(drag_probe):
    """Past a floor, holding the duration would mean a 3-sample teleport. Run long instead."""
    tr = drag_probe(0.200)
    assert len(tr.moves) >= 18, f"{len(tr.moves)} samples — the floor did not hold"


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
