"""The rebalancing decision on in-memory rows — pure, no session anywhere.

Unit = parts of a line. An item of yield 6 on a busy P1S may become three
prints of a 2-part A1 mini plate, or one print of a 12-part X1C plate with a
surplus — whichever finishes first, and only when that beats waiting at home.
"""

from fastapi import HTTPException

from backend.app.services import queue_sources
from backend.app.services.farm_forecast import FarmSnapshot, MachineState, QueuedRow
from backend.app.services.queue_rebalance import (
    SKIP_REASONS,
    FarmView,
    MovableItem,
    PlateOption,
    capture_skip_reason,
    home_wait_by_model,
    plan_moves,
)

H = 3600


def _opt(plate_id, model, yield_parts, seconds=H):
    return PlateOption(
        plate_id=plate_id,
        library_file_id=plate_id,
        plate_index=0,
        model=model,
        model_label=model.upper(),
        yield_parts=yield_parts,
        seconds=seconds,
    )


def _item(item_id, line_id, home, yield_parts, seconds=H):
    return MovableItem(item_id=item_id, line_id=line_id, home_model=home, yield_parts=yield_parts, seconds=seconds)


def _farm(idle, free_at):
    return FarmView(idle_by_model=dict(idle), free_at_by_model=dict(free_at))


def test_same_yield_is_a_plain_file_swap():
    """P1S busy for two more hours, an X1C idle, same plate yield: one conversion, nothing created, no surplus."""
    plan = plan_moves(
        [_item(1, 10, "p1s", 6)],
        {10: [_opt(100, "p1s", 6), _opt(200, "x1c", 6)]},
        _farm({"p1s": 0, "x1c": 1}, {"p1s": 2 * H, "x1c": 0}),
    )
    assert plan.skipped == []
    [move] = plan.moves
    assert (move.item_id, move.from_model, move.to_model, move.plate.plate_id) == (1, "p1s", "x1c", 200)
    assert (move.k, move.surplus, move.moved_parts) == (1, 0, 6)
    assert (move.home_finish, move.finish) == (3 * H, H)


def test_smaller_bed_needs_more_prints_and_moves_only_when_that_is_not_later():
    options = {10: [_opt(100, "p1s", 6), _opt(300, "a1mini", 2, seconds=H)]}
    # Home free in 2h + 1h print = 3h; three mini prints back to back = 3h — not later, so it moves.
    plan = plan_moves([_item(1, 10, "p1s", 6)], options, _farm({"p1s": 0, "a1mini": 1}, {"p1s": 2 * H, "a1mini": 0}))
    [move] = plan.moves
    assert (move.k, move.finish, move.home_finish, move.surplus) == (3, 3 * H, 3 * H, 0)
    # Home free in 1h + 1h = 2h; three mini prints = 3h — later, so it stays.
    plan = plan_moves([_item(1, 10, "p1s", 6)], options, _farm({"p1s": 0, "a1mini": 1}, {"p1s": H, "a1mini": 0}))
    assert plan.moves == [] and plan.skipped == [(1, "no_faster_model")]


def test_two_idle_minis_spread_the_prints_and_both_get_consumed():
    plan = plan_moves(
        [_item(1, 10, "p1s", 6)],
        {10: [_opt(300, "a1mini", 2, seconds=H)]},
        _farm({"p1s": 0, "a1mini": 2}, {"p1s": 5 * H, "a1mini": 0}),
    )
    [move] = plan.moves
    assert (move.k, move.finish) == (3, 2 * H)  # ceil(3 / 2) rounds of an hour


def test_bigger_bed_is_one_print_with_a_bounded_surplus():
    options = {10: [_opt(300, "a1mini", 2), _opt(400, "x1c", 6, seconds=2 * H)]}
    plan = plan_moves([_item(1, 10, "a1mini", 2)], options, _farm({"a1mini": 0, "x1c": 1}, {"a1mini": 3 * H, "x1c": 0}))
    [move] = plan.moves
    assert (move.k, move.surplus, move.finish) == (1, 4, 2 * H)
    plan = plan_moves(
        [_item(1, 10, "a1mini", 2)], options, _farm({"a1mini": 0, "x1c": 1}, {"a1mini": 0.5 * H, "x1c": 0})
    )
    assert plan.moves == []


def test_an_item_whose_home_model_has_an_idle_printer_is_never_moved():
    plan = plan_moves(
        [_item(1, 10, "p1s", 6)],
        {10: [_opt(200, "x1c", 6)]},
        _farm({"p1s": 1, "x1c": 1}, {"p1s": 0, "x1c": 0}),
    )
    assert plan.moves == [] and plan.skipped == [(1, "home_model_idle")]


def test_no_candidate_plate_for_the_idle_model_means_no_move():
    plan = plan_moves(
        [_item(1, 10, "p1s", 6)], {10: [_opt(100, "p1s", 6)]}, _farm({"p1s": 0, "x1c": 1}, {"p1s": 2 * H})
    )
    assert plan.skipped == [(1, "no_faster_model")]


def test_a_home_model_with_no_accepting_machine_always_loses():
    """Every P1S parked: ``free_at`` has no entry, so any move wins."""
    plan = plan_moves(
        [_item(1, 10, "p1s", 6)], {10: [_opt(200, "x1c", 6, seconds=9 * H)]}, _farm({"x1c": 1}, {"x1c": 0})
    )
    [move] = plan.moves
    assert move.home_finish is None and move.finish == 9 * H


def test_lines_share_capacity_in_the_order_given_and_a_cooling_line_is_skipped():
    items = [_item(1, 10, "p1s", 6), _item(2, 20, "p1s", 6), _item(3, 30, "p1s", 6)]
    options = {lid: [_opt(lid * 10, "x1c", 6)] for lid in (10, 20, 30)}
    plan = plan_moves(items, options, _farm({"p1s": 0, "x1c": 1}, {"p1s": 4 * H, "x1c": 0}), cooling={20})
    assert [m.item_id for m in plan.moves] == [1]
    assert plan.skipped == [(2, "cooldown"), (3, "no_faster_model")]


def test_the_best_plate_per_model_is_the_plan_engines_pick_and_a_timeless_plate_is_never_it():
    options = {
        10: [
            _opt(201, "x1c", 6, seconds=2 * H),  # 3 parts/h
            _opt(202, "x1c", 4, seconds=H),  # 4 parts/h — wins, then k = ceil(6/4) = 2
            _opt(203, "x1c", 100, seconds=None),  # no estimate: its finish cannot be compared
        ]
    }
    plan = plan_moves([_item(1, 10, "p1s", 6)], options, _farm({"p1s": 0, "x1c": 1}, {"p1s": 5 * H, "x1c": 0}))
    [move] = plan.moves
    assert (move.plate.plate_id, move.k, move.finish, move.surplus) == (202, 2, 2 * H, 2)


def test_an_item_without_an_estimate_compares_as_instant_at_home():
    options = {10: [_opt(200, "x1c", 6, seconds=H)]}
    plan = plan_moves(
        [_item(1, 10, "p1s", 6, seconds=None)], options, _farm({"p1s": 0, "x1c": 1}, {"p1s": H, "x1c": 0})
    )
    assert [m.item_id for m in plan.moves] == [1]  # H <= H
    plan = plan_moves(
        [_item(1, 10, "p1s", 6, seconds=None)], options, _farm({"p1s": 0, "x1c": 1}, {"p1s": 0.5 * H, "x1c": 0})
    )
    assert plan.moves == []


def test_consumed_capacity_advances_the_receivers_free_at_for_later_items_of_that_model():
    """Three mini prints on one idle mini: the mini is now busy for 3h, so a mini-home
    item that follows compares against 3h + its own hour and moves to the X1C."""
    items = [_item(1, 10, "p1s", 6), _item(2, 20, "a1mini", 2)]
    options = {10: [_opt(300, "a1mini", 2, seconds=H)], 20: [_opt(400, "x1c", 2, seconds=2 * H)]}
    plan = plan_moves(items, options, _farm({"p1s": 0, "a1mini": 1, "x1c": 1}, {"p1s": 9 * H, "a1mini": 0, "x1c": 0}))
    assert [(m.item_id, m.to_model) for m in plan.moves] == [(1, "a1mini"), (2, "x1c")]
    assert plan.moves[1].home_finish == 4 * H


def test_home_wait_is_the_earliest_free_accepting_machine_per_model():
    snapshot = FarmSnapshot(
        printers=[
            MachineState(
                printer_id=1,
                model="P1S",
                queued=[QueuedRow(order_id=None, seconds=1800), QueuedRow(order_id=7, seconds=3600)],
            ),
            MachineState(printer_id=2, model="P1S", queued=[], accepts_new_work=False),  # parked: never the earliest
            # A second accepting P1S, busy for a quarter of an hour: the figure is
            # the EARLIEST such machine, not the first one the snapshot listed.
            MachineState(printer_id=5, model="P1S", queued=[QueuedRow(order_id=None, seconds=900)]),
            MachineState(printer_id=3, model="X1C", queued=[QueuedRow(order_id=None, seconds=None)]),  # unknown = 0
            MachineState(printer_id=4, model=None),
        ],
        staged=[],
    )
    assert home_wait_by_model(snapshot) == {"p1s": 900.0, "x1c": 0.0}


def test_the_reason_list_is_closed():
    assert SKIP_REASONS == (
        "not_found",
        "already_assigned",
        "not_filed",
        "pinned",
        "scheduled",
        "staged",
        "located",
        "no_yield",
        "source_unreadable",
        "source_copy_busy",
        "source_spool_full",
        "creation_failed",
        "home_model_idle",
        "no_faster_model",
        "cooldown",
    )


#: Which bucket each member of the capture taxonomy is reported as (m173). Written
#: out by hand, because the point is that somebody DECIDED: the split is by what the
#: operator does — wait, free space, or fix the file — and not by HTTP status, which
#: two of the buckets share.
_EXPECTED_BUCKETS = {
    "source_copy_busy": "source_copy_busy",
    "source_spool_replaced": "source_copy_busy",
    "source_copy_timeout": "source_copy_busy",
    "source_spool_no_space": "source_spool_full",
    "source_spool_write_failed": "source_spool_full",
    "source_unreadable": "source_unreadable",
    "source_changed": "source_unreadable",
    "source_invalid": "source_unreadable",
    # The base class: an unmapped refusal errs towards "a person must look".
    "source_copy_failed": "source_unreadable",
}


def _taxonomy() -> dict[str, type]:
    """Every refusal class the capture service can raise, read off the module."""
    return {
        value.reason: value
        for value in vars(queue_sources).values()
        if isinstance(value, type) and issubclass(value, queue_sources.QueueSourceError)
    }


def test_every_capture_refusal_is_reported_as_a_decided_bucket():
    """A new refusal class must not silently inherit "the target file cannot be read".

    The taxonomy is walked off the module rather than listed here, so adding a class
    to it fails this test until somebody chooses what the panel should tell the
    operator to do about it.
    """
    assert set(_taxonomy()) == set(_EXPECTED_BUCKETS), (
        "the capture taxonomy changed — decide which bucket the new refusal belongs in"
    )
    for reason, bucket in _EXPECTED_BUCKETS.items():
        detail = {"code": reason, "params": {}, "message": ""}
        assert capture_skip_reason(HTTPException(_taxonomy()[reason].http_status, detail)) == bucket, reason
        assert bucket in SKIP_REASONS


def test_a_refusal_with_no_machine_code_is_still_a_closed_reason():
    """A bare-string detail must not reach the panel as a raw token or a crash."""
    assert capture_skip_reason(HTTPException(422, "something nobody mapped")) == "source_unreadable"
    assert capture_skip_reason(HTTPException(422, {"params": {}})) == "source_unreadable"
