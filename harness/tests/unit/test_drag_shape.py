"""Overshoot is BIMODAL — the property a 12-run coin toss could not actually test.

In the 14 recorded human drags, 7 landed with no overshoot at all and the other 7 ran 5-24px
past before correcting. Always overshooting by a little is the average of two behaviours and
resembles neither, so drag() tosses for which one each gesture is.

drag-profile checked this end to end by counting how many of 12 real drags overshot and
requiring 2..10. That is a fair coin asked to land between 2 and 10 heads in 12 throws: it fails
0.6% of the time on correct code, which was measured, seen once, and — worse — is a rate low
enough to be dismissed as "just re-run it" every time it appears. A test nobody believes is
worse than no test.

The frequency claim lives here instead, where the virtual clock makes 400 drags cost less than a
second, and where 400 samples make a wrong rate unmissable rather than a coin toss. What stays
in drag-profile is the SHAPE — that the overshoots which do happen are real ones and not a small
constant — which cannot flake because it does not depend on how the coin lands.
"""
import statistics


N = 400          # 400 samples: a true rate outside [0.35, 0.65] is a >5-sigma event
LO, HI = 0.35, 0.65


def _sample(drag_probe, n=N):
    return [drag_probe(0.0).overshoot for _ in range(n)]


def test_overshoot_happens_about_half_the_time(drag_probe):
    """The coin must be fair-ish. This is what the 12-run count was reaching for, with enough
    samples that it means something: at n=400 a rate of 0.35 or 0.65 is over 5 sigma out."""
    v = _sample(drag_probe)
    rate = sum(1 for o in v if o >= 1.5) / len(v)
    assert LO <= rate <= HI, f"overshot on {rate:.0%} of {len(v)} drags — the toss is not fair"


def test_both_behaviours_actually_occur(drag_probe):
    """Neither branch may quietly die. At n=400 with a fair coin, seeing zero of either is
    impossible in practice — so this catches a branch that was removed, not a bad run."""
    v = _sample(drag_probe)
    assert any(o >= 1.5 for o in v), "no drag overshot at all — the overshoot branch is dead"
    assert any(o < 1.5 for o in v), "every drag overshot — the clean-landing branch is dead"


def test_overshoot_is_bimodal_not_a_small_constant(drag_probe):
    """THE property. A drag that always ran 2px past would satisfy 'sometimes overshoots' on a
    loose reading while looking like nothing a hand does. The recorded hands leave a gap: either
    ~0, or 5-24px. Nothing lands in between."""
    v = _sample(drag_probe)
    middling = [o for o in v if 1.5 <= o < 4.0]
    assert not middling, f"{len(middling)} drags overshot by a middling amount, e.g. {middling[:5]}"


def test_real_overshoots_match_the_recorded_range(drag_probe):
    """The hands ran 5-24px past. Allow a little for the end-of-path wobble, nothing more."""
    v = [o for o in _sample(drag_probe) if o >= 1.5]
    assert min(v) >= 4.0, f"smallest real overshoot {min(v):.1f}px — under the recorded 5px"
    assert max(v) <= 26.0, f"largest overshoot {max(v):.1f}px — over the recorded 24px"
    assert 5.0 <= statistics.median(v) <= 24.0, f"median overshoot {statistics.median(v):.1f}px"
