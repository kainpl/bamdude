"""The one rank of a placement (C01, owner D3): the printer order after deferred rows, before the remaining level."""

from backend.app.services.printer_occupancy import placement_rank


def rank(*, ready=True, exact=True, colours=1, deferred=False, pid=1, pick=(), remaining=0.0):
    return placement_rank(ready, exact, colours, deferred, pid, pick=pick, remaining_rank=remaining)


def test_pick_decides_before_the_remaining_level():
    assert rank(pid=2, pick=(-1.0,), remaining=-90.0) > rank(pid=1, pick=(-5.0,), remaining=-10.0)


def test_equal_pick_leaves_it_to_the_remaining_level_then_the_lower_id():
    assert rank(pid=2, pick=(-1.0,), remaining=-10.0) > rank(pid=1, pick=(-1.0,), remaining=-90.0)
    assert rank(pid=1, pick=(-1.0,)) > rank(pid=2, pick=(-1.0,))


def test_readiness_model_colours_and_deferred_rows_decide_before_the_pick():
    worse_pick, better_pick = (-9.0,), (-1.0,)
    assert rank(ready=True, pick=worse_pick) > rank(ready=False, pick=better_pick)
    assert rank(exact=True, pick=worse_pick) > rank(exact=False, pick=better_pick)
    assert rank(colours=2, pick=worse_pick) > rank(colours=1, pick=better_pick)
    assert rank(deferred=False, pick=worse_pick) > rank(deferred=True, pick=better_pick)


def test_without_a_pick_the_rank_is_todays():
    assert rank(pid=1) > rank(pid=2)
    assert rank(pid=2, remaining=-10.0) > rank(pid=1, remaining=-90.0)
