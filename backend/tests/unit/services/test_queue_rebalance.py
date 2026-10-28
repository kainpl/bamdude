"""The rebalancing decision on in-memory rows — pure, no session anywhere.

Unit = parts of a line. An item of yield 6 on a busy P1S may become three
prints of a 2-part A1 mini plate, or one print of a 12-part X1C plate with a
surplus — whichever finishes first, and only when that beats waiting at home.

Every printer is asked about the JOB (spec rebalance-job-feasibility r2): a
:class:`_Fits` table says which printer can print which job, so «a home printer
is free» or «a receiver can take it» is always backed by a positive answer here,
never by the printer's model alone.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app.services import queue_sources
from backend.app.services.farm_forecast import QueuedRow, StaggerPolicy
from backend.app.services.queue_rebalance import (
    SKIP_REASONS,
    TARGET_SOURCE_UNREADABLE,
    FarmView,
    Fit,
    Machine,
    MovableItem,
    PassPrinter,
    PlateOption,
    _machines,
    capture_skip_reason,
    plan_moves,
    start_projection,
)
from backend.app.services.rebalance_replay import Held, ReplayBudget
from backend.app.services.stagger_groups import StaggerGroupResolver

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


def test_an_item_without_an_estimate_is_unknown_at_home_never_instant():
    """T19 (C-10 exception, replaces «compares as instant at home»): a job with no estimate ends
    at an unknown time; comparing it as 0 s is what the old forecast did, and no proof."""
    options = {10: [_opt(200, "x1c", 6, seconds=H)]}
    for busy in (H, 0.5 * H):
        plan = plan_moves(
            [_item(1, 10, "p1s", 6, seconds=None)], options, _farm((1, "p1s", False, busy), (2, "x1c", True, 0))
        )
        assert (plan.moves, plan.skipped) == ([], [(1, "forecast_incomplete")])


def test_consumed_capacity_dates_the_receiver_for_a_later_item_whose_home_it_is():
    """Three mini prints for the P1S job, on the one ready mini. C-10 exception (T14, was 4h):
    the converted row keeps its place and the two companions go after every pending row, so the
    mini-home job — queued before them — prints second, from 1h to 2h, and the transferred work
    ends at 4h. Its home finish is 2h, tied with the 2h X1C plate: a tie moves."""
    items = [_item(1, 10, "p1s", 6), _item(2, 20, "a1mini", 2)]
    options = {10: [_opt(300, "a1mini", 2, seconds=H)], 20: [_opt(400, "x1c", 2, seconds=2 * H)]}
    farm = _farm((1, "p1s", False, 9 * H), (2, "a1mini", True, 0), (3, "x1c", True, 0))
    plan = plan_moves(items, options, farm)
    assert [(m.item_id, m.to_model) for m in plan.moves] == [(1, "a1mini"), (2, "x1c")]
    assert (plan.moves[0].finish, plan.moves[1].home_finish) == (4 * H, 2 * H)


def test_rounds_are_written_per_printer():
    """A20: k = 5 X1C prints on two ready X1C end at 3t. C-10 exception (T14, was «dated by
    THAT printer's rounds»): the X1C-home job is queued before the companions, so the router
    serves it before them. When it fits only the printer the converted row took, it waits one
    round and ends at 2t — then a 1 s mini plate takes it; when it fits the other one, the
    router gives it that printer at once and it stays home."""
    t = 1000
    no = Fit("no", "material_mismatch")
    items = [_item(1, 10, "p1s", 5), _item(2, 20, "x1c", 1, seconds=t)]
    options = {10: [_opt(100, "x1c", 1, seconds=t)], 20: [_opt(200, "a1mini", 1, seconds=1)]}
    for fit_on, other in ((2, 3), (3, 2)):
        farm = _farm(
            (1, "p1s", False, 99 * t),
            (2, "x1c", True, 0),
            (3, "x1c", True, 0),
            (4, "a1mini", True, 0),
            fits=_Fits(home={(2, other): no}),
        )
        plan = plan_moves(items, options, farm)
        first = plan.moves[0]
        assert (first.receivers, first.finish) == ((2, 3), 3 * t)
        if fit_on == 2:
            assert plan.moves[1].home_finish == 2 * t
        else:
            assert (len(plan.moves), plan.skipped) == (1, [(2, "home_model_idle")])


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
        "forecast_incomplete",
        "forecast_budget",
        "already_rebalanced_auto",
        "plan_invalidated_after_apply_failure",
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


# ---------- the whole tail and the start times (spec rebalance-tail-time r2) ----------
#
# Every number below is worked out by hand from the scenario, never read back from the
# function under test. The pure farm has no preparation and no plate gap unless a test
# says so, so a print on a printer free at t ends at t + its seconds.


def test_a_job_that_stays_home_holds_its_printer_for_the_next_one():
    """T-A01: one home P2S free in 60 s and four 100 s jobs: at home they end at 160, 260,
    360 and 460 s. A 200 s alternative on one free X1C loses to the first and wins the
    second; after that move the X1C is spent for this pass."""
    items = [_item(i, 10 + i, "p2s", 1, seconds=100) for i in (1, 2, 3, 4)]
    options = {10 + i: [_opt(100 + i, "x1c", 1, seconds=200)] for i in (1, 2, 3, 4)}
    plan = plan_moves(items, options, _farm((1, "p2s", False, 60), (2, "x1c", True, 0)))
    [move] = plan.moves
    assert (move.item_id, move.home_finish, move.finish) == (2, 260, 200)
    assert plan.skipped == [(1, "no_faster_model"), (3, "no_eligible_target"), (4, "no_eligible_target")]


@pytest.mark.parametrize("held", ["cooldown", "no_alternative"])
def test_a_job_that_cannot_move_still_prints_at_home_first(held):
    """T-A02: the job ahead is not a candidate — its line is cooling, or it has no other
    plate — but it is still printed at home before the next one, which therefore ends at
    260 s, not 160 s, and moves to the 200 s alternative."""
    items = [_item(1, 10, "p2s", 1, seconds=100), _item(2, 20, "p2s", 1, seconds=100)]
    options = {20: [_opt(120, "x1c", 1, seconds=200)]}
    if held == "cooldown":
        options[10] = [_opt(110, "x1c", 1, seconds=200)]
    farm = _farm((1, "p2s", False, 60), (2, "x1c", True, 0))
    plan = plan_moves(items, options, farm, cooling={10} if held == "cooldown" else set())
    [move] = plan.moves
    assert (move.item_id, move.home_finish) == (2, 260)
    assert plan.skipped == [(1, "cooldown" if held == "cooldown" else "no_eligible_target")]


def test_the_companions_wait_behind_work_already_queued_for_the_target_model():
    """T-A14 / R5: the writer files the k − 1 companions after every pending row, so an X1C
    job queued ahead of them prints in between: 100 s + its 1000 s + 100 s = 1200 s, not
    200 s — and that loses to a home that ends at 600 s."""
    items = [_item(1, 10, "p2s", 2, seconds=100), _item(2, 20, "x1c", 1, seconds=1000)]
    options = {10: [_opt(110, "x1c", 1, seconds=100)]}  # line 20 has no other plate
    plan = plan_moves(items, options, _farm((1, "p2s", False, 500), (2, "x1c", True, 0)))
    assert plan.moves == []
    assert plan.skipped == [(1, "no_faster_model"), (2, "home_model_idle")]


def test_a_staggered_start_dates_the_second_target_print():
    """T-A11: two 100 s target prints on two ready X1C, one start per 600 s window for the
    whole farm: the second starts at 600 s and ends at 700 s, which loses to a home that
    ends at 260 s (a running print opens no window, the home's own start at 160 s does)."""
    stagger = StaggerPolicy(
        concurrent=1, interval_seconds=600, wait_for_bed=False, resolver=StaggerGroupResolver.global_only()
    )
    farm = _farm((1, "p2s", False, 160), (2, "x1c", True, 0), (3, "x1c", True, 0))
    farm.stagger = stagger
    plan = plan_moves([_item(1, 10, "p2s", 2, seconds=100)], {10: [_opt(110, "x1c", 1, seconds=100)]}, farm)
    assert plan.moves == []
    assert plan.skipped == [(1, "no_faster_model")]


def _budget_farm(budget):
    """Four 100 s P2S jobs behind a home free in 60 s; two ready X1C with a 200 s plate, one
    start per 60 s window: two moves, rejected trials between them, and a spent pool."""
    stagger = StaggerPolicy(
        concurrent=1, interval_seconds=60, wait_for_bed=False, resolver=StaggerGroupResolver.global_only()
    )
    farm = _farm((1, "p2s", False, 60), (2, "x1c", True, 0), (3, "x1c", True, 0))
    farm.stagger = stagger
    farm.budget = budget
    items = [_item(i, 10 + i, "p2s", 1, seconds=100) for i in (1, 2, 3, 4, 5)]
    options = {10 + i: [_opt(100 + i, "x1c", 1, seconds=200)] for i in (1, 2, 3, 4, 5)}
    return items, options, farm


def _verdicts(plan):
    return [(move.item_id, "moved") for move in plan.moves] + list(plan.skipped)


def test_every_budget_gives_each_item_one_verdict_and_never_a_partial_decision():
    """T-A17 / T20: whatever the budget, every item gets exactly one verdict — its unbudgeted one
    or ``forecast_budget`` — the moves made are the first moves of the unbudgeted plan, and once a
    replay runs out no later trial is tried (an earlier candidate is never passed over for a
    later, cheaper one)."""
    items, options, farm = _budget_farm(None)
    projection = start_projection(items, farm)
    full = plan_moves(items, options, farm, projection=projection)
    assert len(full.moves) >= 2 and any(reason == "no_faster_model" for _i, reason in full.skipped)
    unbudgeted = dict(_verdicts(full))
    for budget in range(projection.work + 1):
        items, options, farm = _budget_farm(budget)
        try:
            projection = start_projection(items, farm)
        except ReplayBudget:
            continue
        plan = plan_moves(items, options, farm, projection=projection)
        verdicts = _verdicts(plan)
        assert sorted(item_id for item_id, _v in verdicts) == [item.item_id for item in items], budget
        for item_id, verdict in verdicts:
            assert verdict in (unbudgeted[item_id], "forecast_budget"), (budget, item_id, verdict)
        assert [m.item_id for m in plan.moves] == [m.item_id for m in full.moves[: len(plan.moves)]], budget
        first_out = next((i for i, (_id, v) in enumerate(plan.skipped) if v == "forecast_budget"), None)
        if first_out is not None:
            later = {item_id for item_id, _v in plan.skipped[first_out:]}
            assert not later & {m.item_id for m in plan.moves}, budget
    items, options, farm = _budget_farm(projection.work + 10_000)
    assert _verdicts(plan_moves(items, options, farm)) == _verdicts(full)


def _state(pid, *queued):
    return SimpleNamespace(
        printer_id=pid,
        model="P1S",
        queued=list(queued),
        plate_clear_seconds=0,
        waiting_seconds=0,
        awaiting_operator=False,
        requires_operator=False,
        stagger_interval_seconds=None,
    )


def test_what_a_printer_holds_is_read_as_the_router_reads_it():
    """T12 / T19 / T-A25: a routable printer is free now exactly when it is outside the router's
    busy set, and what has no forecast release is unknown — never 0, never dropped."""
    running_over = QueuedRow(order_id=None, seconds=0, started=True)
    queued = QueuedRow(order_id=None, seconds=600, prep_seconds=120)
    snapshot = SimpleNamespace(
        printers=[
            _state(1),  # claimed for a print whose archive does not exist yet
            _state(2, running_over, queued),  # claimed, printing past its estimate, a job behind it
            _state(3, QueuedRow(order_id=None, seconds=0)),  # a queued job estimated at nothing
            _state(4, running_over),  # past its estimate and no claim left: over
            _state(5, QueuedRow(order_id=None, seconds=900, started=True)),  # printing, no claim
            _state(6, QueuedRow(order_id=None, seconds=900, started=True)),  # printing, claimed
        ],
        order_resources={},
        operator_hours=None,
    )
    printers = {pid: PassPrinter(pid, "P1S", "p1s", None, False) for pid in range(1, 7)}
    machines = _machines(snapshot, printers, busy={1, 2, 3, 6}, interlocked=set())
    assert machines[1].held == (Held(None, started=True),) and not machines[1].hold_unknown
    assert machines[2].held == (Held(None, started=True), Held(600.0, prep=120.0))
    assert machines[3].held == (Held(None),)
    assert machines[4].held == () and not machines[4].hold_unknown
    assert machines[5].held == () and machines[5].hold_unknown
    assert machines[6].held == (Held(900.0, started=True),) and not machines[6].hold_unknown


def test_an_unknown_home_holds_its_own_job_and_no_independent_one():
    """T-A17 / T18: job 1's home P2S is held with no known release — its finish is unknown, so it
    stays (``forecast_incomplete``). Job 2 shares no printer and no start group with it: its
    busy A1 ends in 5 h, a free P1S does it in 1 h, and it moves."""
    farm = _farm((2, "x1c", True, 0), (3, "a1", False, 5 * H), (4, "p1s", True, 0))
    farm.machines[1] = Machine(1, "p2s", False, hold_unknown=True)
    plan = plan_moves(
        [_item(1, 10, "p2s", 1), _item(2, 20, "a1", 1)],
        {10: [_opt(110, "x1c", 1)], 20: [_opt(120, "p1s", 1)]},
        farm,
    )
    assert plan.skipped == [(1, "forecast_incomplete")]
    [move] = plan.moves
    assert (move.item_id, move.to_model, move.home_finish, move.finish) == (2, "p1s", 6 * H, H)


# ---------- T-A27 / T-A28: the shortcuts change no decision ----------

MODELS = ("p1s", "p2s", "x1c")


def _random_case(seed):
    """A small random farm: printers free, busy, holding a queue, held with no release, or
    awaiting their plate; candidates with and without estimates; a tail; alternative plates;
    printers that cannot print some jobs or cannot yet answer; SJF, compatible models and a
    staggered start each on or off."""
    import random

    rng = random.Random(seed)
    machines = {}
    for pid in range(1, rng.randint(2, 7) + 1):
        model = rng.choice(MODELS)
        kind = rng.random()
        if kind < 0.4:
            machines[pid] = Machine(pid, model, True)
        elif kind < 0.65:
            machines[pid] = Machine(pid, model, False, float(rng.choice([50, 120, 300, 600, 900])))
        elif kind < 0.8:
            held = (Held(float(rng.choice([100, 400])), started=True), Held(float(rng.choice([200, 500]))))
            machines[pid] = Machine(pid, model, False, held=held)
        elif kind < 0.88:
            machines[pid] = Machine(pid, model, False, hold_unknown=True)
        else:
            machines[pid] = Machine(pid, model, False, waiting=float(rng.choice([60, 300])))
    seconds = [None, 100, 300, 600, 1200]
    items = []
    options = {}
    for i in range(1, rng.randint(1, 5) + 1):
        home = rng.choice(MODELS)
        items.append(
            MovableItem(
                item_id=i,
                line_id=100 + i,
                home_model=home,
                yield_parts=rng.randint(1, 4),
                seconds=rng.choice(seconds[1:]) if rng.random() < 0.9 else None,
                position=rng.randint(0, 10),
                group=home.upper(),
            )
        )
        options[100 + i] = [
            PlateOption(
                plate_id=1000 + 10 * i + j,
                library_file_id=1000 + 10 * i + j,
                plate_index=0,
                model=(model := rng.choice(MODELS)),
                model_label=model.upper(),
                yield_parts=rng.randint(1, 4),
                seconds=rng.choice([100, 200, 400, 800]),
            )
            for j in range(rng.randint(0, 3))
        ]
    tail = [
        MovableItem(
            item_id=50 + j,
            line_id=None,
            home_model=(home := rng.choice(MODELS)),
            yield_parts=0,
            seconds=rng.choice(seconds),
            position=rng.randint(0, 10),
            group=home.upper(),
        )
        for j in range(rng.randint(0, 6))
    ]
    verdicts = [Fit("no", "material_mismatch"), Fit("unknown", "feed_unknown")]
    home_fits = {
        (job.item_id, pid): rng.choice(verdicts) for job in (*items, *tail) for pid in machines if rng.random() < 0.15
    }
    target_fits = {
        (item.item_id, plate.plate_id, pid): rng.choice(verdicts)
        for item in items
        for plate in options[item.line_id]
        for pid in machines
        if rng.random() < 0.15
    }
    farm = FarmView(
        machines=machines,
        fits=_Fits(home=home_fits, target=target_fits),
        allow_compatible=rng.random() < 0.3,
        tail=tuple(tail),
        sjf=rng.random() < 0.5,
        stagger=None
        if rng.random() < 0.5
        else StaggerPolicy(
            concurrent=rng.randint(1, 2),
            interval_seconds=rng.choice([100, 300, 600]),
            wait_for_bed=rng.random() < 0.3,
            resolver=StaggerGroupResolver.global_only(),
        ),
    )
    rng.shuffle(items)
    return items, options, farm


def _decided(items, options, farm):
    """The plan and, after it, every job's forecast in the pass's final world."""
    projection = start_projection(items, farm)
    plan = plan_moves(items, options, farm, projection=projection)
    world = {}
    for job_id in sorted(projection.jobs):
        replayed = projection.result_of(job_id)
        world[job_id] = (replayed.outcome(job_id), replayed.lane_of.get(job_id), replayed.routed_at.get(job_id))
    return plan, world


def _one_part(lanes, jobs, **_kw):
    return [(frozenset(lane.printer_id for lane in lanes), frozenset(job.job_id for job in jobs))]


@pytest.mark.parametrize("seed", range(300))
def test_the_parts_change_no_decision(seed, monkeypatch):
    """T-A28 (T21.3): replaying only the part of the farm a trial touches gives the same moves,
    the same reasons and the same forecast of every job as replaying the whole farm."""
    from backend.app.services import queue_rebalance

    items, options, farm = _random_case(seed)
    reference = _decided(items, options, farm)
    monkeypatch.setattr(queue_rebalance, "components", _one_part)
    assert _decided(items, options, farm) == reference


@pytest.mark.parametrize("seed", range(300))
def test_the_lower_bound_spares_trials_and_changes_no_move(seed, monkeypatch):
    """T-A27 (T21.1): pruning a plate by its lower bound gives the same moves and the same
    forecast of every job as trying it. The one reason it may change is for the better: a plate
    whose trial would end unknown but whose bound already ends after the home finish is proven
    slower — ``no_faster_model`` — where the trial alone could only say ``forecast_incomplete``."""
    from backend.app.services import queue_rebalance

    items, options, farm = _random_case(seed)
    pruned, world = _decided(items, options, farm)
    monkeypatch.setattr(queue_rebalance, "_lower_bound", lambda k, c, seconds: float("-inf"))
    tried, tried_world = _decided(items, options, farm)
    assert (pruned.moves, world) == (tried.moves, tried_world)
    assert [item_id for item_id, _r in pruned.skipped] == [item_id for item_id, _r in tried.skipped]
    for (_id, reason), (_same, without) in zip(pruned.skipped, tried.skipped, strict=True):
        assert reason == without or (reason, without) == ("no_faster_model", "forecast_incomplete")


def test_a_lower_bound_equal_to_the_home_finish_still_tries_the_plate():
    """T21.1, strict ``>``: two 100 s prints on one free X1C need 200 s at least; the home ends
    at 200 s. A tie is allowed (T16), so the plate is tried — and taken."""
    plan = plan_moves(
        [_item(1, 10, "p2s", 2, seconds=100)],
        {10: [_opt(110, "x1c", 1, seconds=100)]},
        _farm((1, "p2s", False, 100), (2, "x1c", True, 0)),
    )
    [move] = plan.moves
    assert (move.k, move.home_finish, move.finish) == (2, 200, 200)


def test_taking_the_original_off_its_home_frees_a_start_window_the_target_waited_for():
    """T21.1 / C-2: one start per 500 s for the whole farm. In the baseline the X1C job U
    (600 s) starts at 0 and the candidate waits for the window at home: it starts at 500 and
    ends at 800. Moved, the candidate prints first on the X1C and ends at 300. A bound taken
    from the target's baseline clock (U done at 600, + 300 = 900 > 800) would have thrown the
    move away; the bound from now (300) does not."""
    farm = _farm((1, "p2s", False, 100), (2, "x1c", True, 0))
    farm.stagger = StaggerPolicy(
        concurrent=1, interval_seconds=500, wait_for_bed=False, resolver=StaggerGroupResolver.global_only()
    )
    farm.tail = (MovableItem(50, None, "x1c", 0, seconds=600, position=2, group="X1C"),)
    candidate = MovableItem(1, 10, "p2s", 1, seconds=300, position=1, group="P2S")
    plan = plan_moves([candidate], {10: [_opt(110, "x1c", 1, seconds=300)]}, farm)
    [move] = plan.moves
    assert (move.home_finish, move.finish) == (800, 300)


def test_the_random_farms_exercise_the_shortcuts(monkeypatch):
    """The differential above is not vacuous: across its farms the lower bound spares trials,
    trials run, moves are made and the farm splits into several parts."""
    from backend.app.services import queue_rebalance

    spared = trials = moves = split = 0
    for seed in range(300):
        items, options, farm = _random_case(seed)
        projection = start_projection(items, farm)
        split += len(projection._parts) > 1
        before = projection.work
        plan = plan_moves(items, options, farm, projection=projection)
        trials += projection.work > before
        moves += len(plan.moves)
        with monkeypatch.context() as patched:
            patched.setattr(queue_rebalance, "_lower_bound", lambda k, c, seconds: float("-inf"))
            unpruned = start_projection(items, farm)
            plan_moves(items, options, farm, projection=unpruned)
        spared += unpruned.work > projection.work
    # A one-print plate the bound throws away is a trial the latest-start cut ends at once anyway,
    # so the bound now spares work only where a plate takes several rounds.
    assert spared > 3 and trials > 50 and moves > 50 and split > 100, (spared, trials, moves, split)


def test_the_plan_says_what_the_forecast_gave_each_job_at_home():
    """Stage 5, for the explanation (package D): the plan carries every candidate's home outcome
    as the decision read it — a known finish, unknown, or no printer of its own that can print
    it — and nothing for one the pass had no forecast left for."""
    no = Fit("no", "material_mismatch")
    farm = _farm((1, "p2s", False, 60), (2, "x1c", True, 0), (3, "a1", True, 0), fits=_Fits(home={(3, 3): no}))
    items = [
        _item(1, 10, "p2s", 1, seconds=100),
        _item(2, 20, "p2s", 1, seconds=None),
        _item(3, 30, "a1", 1),
    ]
    plan = plan_moves(items, {}, farm)
    assert plan.home == {1: ("known", 160.0), 2: ("unknown", None), 3: ("none", None)}


def test_the_collector_waits_while_a_pass_decides_and_comes_back_once():
    """T22: a full collection stops every thread, the event loop's too; the decision makes no
    reference cycles, so the cyclic collector waits while it runs. Two passes deciding at once
    hand it back once, when the last one ends — and a collector somebody else switched off
    stays off."""
    import gc

    from backend.app.services.queue_rebalance import _collector_paused

    assert gc.isenabled()
    with _collector_paused():
        assert not gc.isenabled()
        with _collector_paused():
            assert not gc.isenabled()
        assert not gc.isenabled()
    assert gc.isenabled()
    gc.disable()
    try:
        with _collector_paused():
            pass
        assert not gc.isenabled()
    finally:
        gc.enable()


def test_a_trial_stops_once_its_print_can_no_longer_start_in_time(monkeypatch):
    """T20 / T21: a moved print that has not started by home − (preparation + its time) ends
    after home whatever comes next, so the trial needs no clock beyond that start."""
    from backend.app.services import queue_rebalance

    asked = []
    real = queue_rebalance._Projection.trial

    def trial(self, jobs, transferred, *, until=None):
        asked.append(until)
        return real(self, jobs, transferred, until=until)

    monkeypatch.setattr(queue_rebalance._Projection, "trial", trial)
    item = MovableItem(1, 10, "p2s", 1, seconds=3000, prep=120.0)
    plan_moves([item], {10: [_opt(110, "x1c", 1, seconds=1000)]}, _farm((1, "p2s", False, 400), (2, "x1c", True, 0)))
    assert asked == [400 + 120 + 3000 - (120 + 1000)]
