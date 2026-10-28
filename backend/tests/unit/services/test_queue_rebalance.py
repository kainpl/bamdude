"""The rebalancing decision on in-memory rows — pure, no session anywhere.

Unit = parts of a line. An item of yield 6 on a busy P1S may become three
prints of a 2-part A1 mini plate, or one print of a 12-part X1C plate with a
surplus — whichever finishes first, and only when that beats waiting at home.

Every printer is asked about the JOB (spec rebalance-job-feasibility r2): a
:class:`_Fits` table says which printer can print which job, so «a home printer
is free» or «a receiver can take it» is always backed by a positive answer here,
never by the printer's model alone.
"""

from fastapi import HTTPException

from backend.app.services import queue_sources
from backend.app.services.farm_forecast import FarmSnapshot, MachineState, QueuedRow
from backend.app.services.queue_rebalance import (
    SKIP_REASONS,
    TARGET_SOURCE_UNREADABLE,
    FarmView,
    Fit,
    Machine,
    MovableItem,
    PlateOption,
    capture_skip_reason,
    free_after_by_printer,
    plan_moves,
)

H = 3600
YES = Fit("yes")


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


class _Fits:
    """Which printer can print which job. Everything is ``yes`` unless named otherwise."""

    def __init__(self, home=None, target=None):
        self._home = home or {}  # (item_id, printer_id) → Fit
        self._target = target or {}  # (item_id, plate_id, printer_id) → Fit

    def home(self, item_id, printer_id):
        return self._home.get((item_id, printer_id), YES)

    def target(self, item_id, plate, printer_id):
        return self._target.get((item_id, plate.plate_id, printer_id), YES)


def _farm(*machines, fits=None, allow_compatible=False):
    """``machines`` = ``(printer_id, model, ready, free_after)``; a ready printer's seed is 0 whatever it says."""
    return FarmView(
        machines={pid: Machine(pid, model, ready, float(free)) for pid, model, ready, free in machines},
        fits=fits or _Fits(),
        allow_compatible=allow_compatible,
    )


def test_same_yield_is_a_plain_file_swap():
    """P1S busy for two more hours, an X1C ready, same plate yield: one conversion, nothing created, no surplus."""
    plan = plan_moves(
        [_item(1, 10, "p1s", 6)],
        {10: [_opt(100, "p1s", 6), _opt(200, "x1c", 6)]},
        _farm((1, "p1s", False, 2 * H), (2, "x1c", True, 0)),
    )
    assert plan.skipped == []
    [move] = plan.moves
    assert (move.item_id, move.from_model, move.to_model, move.plate.plate_id) == (1, "p1s", "x1c", 200)
    assert (move.k, move.surplus, move.moved_parts, move.receivers) == (1, 0, 6, (2,))
    assert (move.home_finish, move.finish) == (3 * H, H)


def test_smaller_bed_needs_more_prints_and_moves_only_when_that_is_not_later():
    options = {10: [_opt(100, "p1s", 6), _opt(300, "a1mini", 2, seconds=H)]}
    # Home free in 2h + 1h print = 3h; three mini prints back to back = 3h — not later, so it moves.
    plan = plan_moves([_item(1, 10, "p1s", 6)], options, _farm((1, "p1s", False, 2 * H), (2, "a1mini", True, 0)))
    [move] = plan.moves
    assert (move.k, move.finish, move.home_finish, move.surplus) == (3, 3 * H, 3 * H, 0)
    # Home free in 1h + 1h = 2h; three mini prints = 3h — later, so it stays.
    plan = plan_moves([_item(1, 10, "p1s", 6)], options, _farm((1, "p1s", False, H), (2, "a1mini", True, 0)))
    assert plan.moves == [] and plan.skipped == [(1, "no_faster_model")]


def test_two_ready_minis_spread_the_prints():
    plan = plan_moves(
        [_item(1, 10, "p1s", 6)],
        {10: [_opt(300, "a1mini", 2, seconds=H)]},
        _farm((1, "p1s", False, 5 * H), (2, "a1mini", True, 0), (3, "a1mini", True, 0)),
    )
    [move] = plan.moves
    assert (move.k, move.finish, move.receivers) == (3, 2 * H, (2, 3))  # ceil(3 / 2) rounds of an hour


def test_bigger_bed_is_one_print_with_a_bounded_surplus():
    options = {10: [_opt(300, "a1mini", 2), _opt(400, "x1c", 6, seconds=2 * H)]}
    plan = plan_moves([_item(1, 10, "a1mini", 2)], options, _farm((1, "a1mini", False, 3 * H), (2, "x1c", True, 0)))
    [move] = plan.moves
    assert (move.k, move.surplus, move.finish) == (1, 4, 2 * H)
    plan = plan_moves([_item(1, 10, "a1mini", 2)], options, _farm((1, "a1mini", False, 0.5 * H), (2, "x1c", True, 0)))
    assert plan.moves == []


def test_a_ready_home_printer_that_can_print_the_job_keeps_it():
    plan = plan_moves(
        [_item(1, 10, "p1s", 6)],
        {10: [_opt(200, "x1c", 6)]},
        _farm((1, "p1s", True, 0), (2, "x1c", True, 0)),
    )
    assert plan.moves == [] and plan.skipped == [(1, "home_model_idle")]


def test_a_ready_home_printer_that_cannot_print_the_job_neither_holds_it_nor_dates_it():
    """A01, pure: the free P2S holds no PETG. It is not «home is idle», and it is not «home is free
    now» either — with no fit home at all the target needs only to exist, however slow."""
    no = Fit("no", "material_mismatch")
    plan = plan_moves(
        [_item(1, 10, "p2s", 6)],
        {10: [_opt(200, "p1s", 6, seconds=5 * H)]},
        _farm((1, "p2s", True, 0), (2, "p1s", True, 0), fits=_Fits(home={(1, 1): no})),
    )
    [move] = plan.moves
    assert (move.to_model, move.home_finish, move.finish) == ("p1s", None, 5 * H)


def test_a_busy_home_that_can_print_the_job_dates_it_and_one_that_cannot_does_not():
    """A04: an unfit idle P2S and a fit P2S busy for 2h — home is the busy one's 2h + 1h."""
    no = Fit("no", "material_mismatch")
    options = {10: [_opt(200, "p1s", 6, seconds=2 * H)]}
    farm = _farm((1, "p2s", True, 0), (2, "p2s", False, 2 * H), (3, "p1s", True, 0), fits=_Fits(home={(1, 1): no}))
    [move] = plan_moves([_item(1, 10, "p2s", 6)], options, farm).moves
    assert (move.home_finish, move.finish) == (3 * H, 2 * H)
    # A slower target loses to that home.
    slower = {10: [_opt(200, "p1s", 6, seconds=4 * H)]}
    assert plan_moves([_item(1, 10, "p2s", 6)], slower, farm).skipped == [(1, "no_faster_model")]
    # And the busy P2S, once it cannot print the job either, dates nothing.
    both_no = _Fits(home={(1, 1): no, (1, 2): no})
    farm = _farm((1, "p2s", True, 0), (2, "p2s", False, 2 * H), (3, "p1s", True, 0), fits=both_no)
    [move] = plan_moves([_item(1, 10, "p2s", 6)], slower, farm).moves
    assert move.home_finish is None


def test_compatible_ready_printer_holds_the_job_only_when_enabled():
    item = _item(1, 10, "p1p", 6)
    options = {10: [_opt(200, "x1c", 6)]}
    assert plan_moves([item], options, _farm((1, "p1s", True, 0), (2, "x1c", True, 0))).moves
    farm = _farm((1, "p1s", True, 0), (2, "x1c", True, 0), allow_compatible=True)
    assert plan_moves([item], options, farm).skipped == [(1, "home_model_idle")]


def test_no_candidate_plate_for_another_model_means_no_eligible_target():
    plan = plan_moves(
        [_item(1, 10, "p1s", 6)],
        {10: [_opt(100, "p1s", 6)]},
        _farm((1, "p1s", False, 2 * H), (2, "x1c", True, 0)),
    )
    assert plan.skipped == [(1, "no_eligible_target")]


def test_a_home_model_with_no_accepting_machine_always_loses():
    """Every P1S parked or offline: none is in the farm, so any move wins."""
    plan = plan_moves([_item(1, 10, "p1s", 6)], {10: [_opt(200, "x1c", 6, seconds=9 * H)]}, _farm((2, "x1c", True, 0)))
    [move] = plan.moves
    assert move.home_finish is None and move.finish == 9 * H


def test_lines_share_capacity_in_the_order_given_and_a_cooling_line_is_skipped():
    items = [_item(1, 10, "p1s", 6), _item(2, 20, "p1s", 6), _item(3, 30, "p1s", 6)]
    options = {lid: [_opt(lid * 10, "x1c", 6)] for lid in (10, 20, 30)}
    plan = plan_moves(items, options, _farm((1, "p1s", False, 4 * H), (2, "x1c", True, 0)), cooling={20})
    assert [m.item_id for m in plan.moves] == [1]
    assert plan.skipped == [(2, "cooldown"), (3, "no_eligible_target")]


def test_the_best_plate_per_model_is_the_plan_engines_pick_and_a_timeless_plate_is_never_it():
    options = {
        10: [
            _opt(201, "x1c", 6, seconds=2 * H),  # 3 parts/h
            _opt(202, "x1c", 4, seconds=H),  # 4 parts/h — wins, then k = ceil(6/4) = 2
            _opt(203, "x1c", 100, seconds=None),  # no estimate: its finish cannot be compared
        ]
    }
    plan = plan_moves([_item(1, 10, "p1s", 6)], options, _farm((1, "p1s", False, 5 * H), (2, "x1c", True, 0)))
    [move] = plan.moves
    assert (move.plate.plate_id, move.k, move.finish, move.surplus) == (202, 2, 2 * H, 2)


def test_an_unfit_best_plate_does_not_hide_a_fit_second_one():
    """A06: the plan engine's favourite X1C plate needs a filament no X1C holds; the second can be printed."""
    no = Fit("no", "material_mismatch")
    options = {10: [_opt(201, "x1c", 4, seconds=H), _opt(202, "x1c", 6, seconds=2 * H)]}
    farm = _farm((1, "p1s", False, 5 * H), (2, "x1c", True, 0), fits=_Fits(target={(1, 201, 2): no}))
    [move] = plan_moves([_item(1, 10, "p1s", 6)], options, farm).moves
    assert (move.plate.plate_id, move.k, move.finish) == (202, 1, 2 * H)


def test_each_plate_counts_only_its_own_fit_receivers():
    """A07: two X1C plates of one line, each printable on a different X1C: capacity is the CHOSEN plate's."""
    no = Fit("no", "material_mismatch")
    options = {10: [_opt(201, "x1c", 2, seconds=H)]}
    fits = _Fits(target={(1, 201, 3): no})
    farm = _farm((1, "p1s", False, 9 * H), (2, "x1c", True, 0), (3, "x1c", True, 0), fits=fits)
    [move] = plan_moves([_item(1, 10, "p1s", 6)], options, farm).moves
    # Three prints on the one X1C that can print the plate: 3 h, not ceil(3/2) = 2 h.
    assert (move.k, move.receivers, move.finish) == (3, (2,), 3 * H)


def test_an_item_without_an_estimate_compares_as_instant_at_home():
    options = {10: [_opt(200, "x1c", 6, seconds=H)]}
    plan = plan_moves([_item(1, 10, "p1s", 6, seconds=None)], options, _farm((1, "p1s", False, H), (2, "x1c", True, 0)))
    assert [m.item_id for m in plan.moves] == [1]  # H <= H
    plan = plan_moves(
        [_item(1, 10, "p1s", 6, seconds=None)], options, _farm((1, "p1s", False, 0.5 * H), (2, "x1c", True, 0))
    )
    assert plan.moves == []


def test_consumed_capacity_dates_the_receiver_for_a_later_item_whose_home_it_is():
    """Three mini prints on one ready mini: the mini is now busy for 3h, so a mini-home
    item that follows compares against 3h + its own hour and moves to the X1C."""
    items = [_item(1, 10, "p1s", 6), _item(2, 20, "a1mini", 2)]
    options = {10: [_opt(300, "a1mini", 2, seconds=H)], 20: [_opt(400, "x1c", 2, seconds=2 * H)]}
    farm = _farm((1, "p1s", False, 9 * H), (2, "a1mini", True, 0), (3, "x1c", True, 0))
    plan = plan_moves(items, options, farm)
    assert [(m.item_id, m.to_model) for m in plan.moves] == [(1, "a1mini"), (2, "x1c")]
    assert plan.moves[1].home_finish == 4 * H


def test_rounds_are_written_per_printer():
    """A20: k = 5 on two receivers writes 3t on the first and 2t on the second — a later
    X1C-home item that only one of them can print is dated by THAT printer's rounds."""
    t = 1000
    no = Fit("no", "material_mismatch")
    items = [_item(1, 10, "p1s", 5), _item(2, 20, "x1c", 1, seconds=t)]
    options = {10: [_opt(100, "x1c", 1, seconds=t)], 20: [_opt(200, "a1mini", 1, seconds=1)]}
    for _fit_on, other, expected in ((2, 3, 3 * t), (3, 2, 2 * t)):
        farm = _farm(
            (1, "p1s", False, 99 * t),
            (2, "x1c", True, 0),
            (3, "x1c", True, 0),
            (4, "a1mini", True, 0),
            fits=_Fits(home={(2, other): no}),
        )
        first, second = plan_moves(items, options, farm).moves
        assert (first.receivers, first.finish) == ((2, 3), 3 * t)
        assert second.home_finish == expected + t


def test_one_print_takes_one_printer_and_leaves_the_others_free():
    """A20: k = 1 with three ready receivers occupies one; the next item still has two."""
    items = [_item(1, 10, "p1s", 1), _item(2, 20, "p1s", 2)]
    options = {10: [_opt(100, "x1c", 1)], 20: [_opt(200, "x1c", 1)]}
    farm = _farm((1, "p1s", False, 9 * H), (2, "x1c", True, 0), (3, "x1c", True, 0), (4, "x1c", True, 0))
    plan = plan_moves(items, options, farm)
    assert [m.receivers for m in plan.moves] == [(2,), (3, 4)]


def test_receivers_are_ranked_as_the_router_ranks_them_and_input_order_does_not_matter():
    """Exact model before compatible, then more colour matches, then the lower id."""
    fits = _Fits(target={(1, 100, 2): Fit("yes", None, 1), (1, 100, 3): Fit("yes", None, 2), (1, 100, 4): YES})
    options = {10: [_opt(100, "x1c", 1)]}
    machines = [(1, "p1s", False, 9 * H), (4, "x1c", True, 0), (3, "x1c", True, 0), (2, "x1c", True, 0)]
    for order in (machines, list(reversed(machines))):
        [move] = plan_moves([_item(1, 10, "p1s", 3)], options, _farm(*order, fits=fits)).moves
        assert move.receivers == (3, 2, 4)
    # A compatible printer ranks after every exact one.
    farm = _farm((1, "p2s", False, 9 * H), (5, "p1s", True, 0), (6, "p1p", True, 0), allow_compatible=True)
    [move] = plan_moves([_item(1, 10, "p2s", 2)], {10: [_opt(100, "p1s", 1)]}, farm).moves
    assert move.receivers == (5, 6)


def test_a_printer_is_capacity_once_across_plates_and_models():
    """A07/A09: one X1C that is also a compatible receiver of the P1S plate is used by one move only."""
    items = [_item(1, 10, "p2s", 1), _item(2, 20, "p2s", 1)]
    options = {10: [_opt(100, "x1c", 1)], 20: [_opt(200, "p1s", 1)]}
    farm = _farm((1, "p2s", False, 9 * H), (2, "x1c", True, 0), allow_compatible=True)
    plan = plan_moves(items, options, farm)
    assert [m.item_id for m in plan.moves] == [1]
    assert plan.skipped == [(2, "no_eligible_target")]


def test_a_not_chosen_alternative_consumes_nothing():
    """A20: the slower model's receiver is still free for the next item."""
    items = [_item(1, 10, "p1s", 1), _item(2, 20, "p1s", 1)]
    options = {
        10: [_opt(100, "x1c", 1, seconds=H), _opt(101, "a1mini", 1, seconds=2 * H)],
        20: [_opt(200, "a1mini", 1)],
    }
    farm = _farm((1, "p1s", False, 9 * H), (2, "x1c", True, 0), (3, "a1mini", True, 0))
    plan = plan_moves(items, options, farm)
    assert [(m.item_id, m.to_model, m.receivers) for m in plan.moves] == [(1, "x1c", (2,)), (2, "a1mini", (3,))]


def test_a_forecast_zero_is_not_readiness():
    """A19: a home awaiting its plate clear, or busy for an unknown time, may be forecast at 0 s —
    it dates the home, but neither holds the job like a ready printer nor receives work."""
    options = {10: [_opt(200, "x1c", 6, seconds=H)]}
    plan = plan_moves([_item(1, 10, "p1s", 6)], options, _farm((1, "p1s", False, 0), (2, "x1c", True, 0)))
    [move] = plan.moves
    assert move.home_finish == H  # 0 s + its own hour; the X1C's hour is not later
    # A not-ready X1C at 0 s receives nothing.
    plan = plan_moves([_item(1, 10, "p1s", 6)], options, _farm((1, "p1s", False, 2 * H), (2, "x1c", False, 0)))
    assert plan.skipped == [(1, "no_eligible_target")]


def test_an_unknown_answer_holds_a_ready_home_only():
    """R8: unknown on a ready home printer → eligibility_unknown; on a busy one → nothing at all."""
    unknown = Fit("unknown", "feed_state_unavailable")
    options = {10: [_opt(200, "x1c", 6, seconds=5 * H)]}
    farm = _farm((1, "p1s", True, 0), (2, "x1c", True, 0), fits=_Fits(home={(1, 1): unknown}))
    assert plan_moves([_item(1, 10, "p1s", 6)], options, farm).skipped == [(1, "eligibility_unknown")]
    # A fit ready home still wins over another, unknown one.
    farm = _farm((1, "p1s", True, 0), (3, "p1s", True, 0), (2, "x1c", True, 0), fits=_Fits(home={(1, 1): unknown}))
    assert plan_moves([_item(1, 10, "p1s", 6)], options, farm).skipped == [(1, "home_model_idle")]
    # Busy and unknown: no veto, no date, no skip — the target only has to exist.
    farm = _farm((1, "p1s", False, H), (2, "x1c", True, 0), fits=_Fits(home={(1, 1): unknown}))
    [move] = plan_moves([_item(1, 10, "p1s", 6)], options, farm).moves
    assert move.home_finish is None


def test_an_unknown_target_gives_no_capacity_and_says_why():
    options = {10: [_opt(200, "x1c", 6)]}
    unknown = _Fits(target={(1, 200, 2): Fit("unknown", "nozzle_state_unavailable")})
    farm = _farm((1, "p1s", False, 2 * H), (2, "x1c", True, 0), fits=unknown)
    assert plan_moves([_item(1, 10, "p1s", 6)], options, farm).skipped == [(1, "eligibility_unknown")]
    unreadable = _Fits(target={(1, 200, 2): Fit("unknown", TARGET_SOURCE_UNREADABLE)})
    farm = _farm((1, "p1s", False, 2 * H), (2, "x1c", True, 0), fits=unreadable)
    assert plan_moves([_item(1, 10, "p1s", 6)], options, farm).skipped == [(1, "source_unreadable")]


def test_model_compatibility_is_asked_once_per_pair_of_models(monkeypatch):
    """Review I2: with compatible models ON, 1000 jobs × 32 printers must not ask the compatibility
    matrix per job and printer — a handful of model pairs exist, and the decision runs on the loop."""
    from backend.app.services import queue_rebalance

    calls = []
    real = queue_rebalance.model_compatibility

    def counting(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(queue_rebalance, "model_compatibility", counting)
    items = [_item(i, 10, "p2s", 1) for i in range(1, 201)]
    options = {10: [_opt(100, "p1s", 1), _opt(101, "x1c", 1)]}
    machines = [(pid, "p1s" if pid % 2 else "x1c", pid > 4, 0 if pid > 4 else 9 * H) for pid in range(1, 33)]
    plan_moves(items, options, _farm(*machines, allow_compatible=True))
    assert calls and len(calls) == len(set(calls)), f"{len(calls)} calls for {len(set(calls))} pairs"


def test_free_after_is_each_printers_running_print_and_queue():
    snapshot = FarmSnapshot(
        printers=[
            MachineState(
                printer_id=1,
                model="P1S",
                queued=[QueuedRow(order_id=None, seconds=1800), QueuedRow(order_id=7, seconds=3600)],
            ),
            MachineState(printer_id=2, model="P1S", running_seconds=600.0, queued=[], accepts_new_work=False),
            MachineState(printer_id=3, model="X1C", queued=[QueuedRow(order_id=None, seconds=None)]),  # unknown = 0
            MachineState(printer_id=4, model=None, running_seconds=-5.0),
        ],
        staged=[],
    )
    assert free_after_by_printer(snapshot) == {1: 5400.0, 2: 600.0, 3: 0.0, 4: 0.0}


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
        "order_resources",
        "eligibility_unknown",
        "no_eligible_target",
        "eligibility_changed",
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
