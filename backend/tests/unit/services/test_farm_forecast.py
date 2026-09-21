"""The forecast core on in-memory rows — pure, ``now`` injected, no session.

Routing is not dispatching: nothing here knows a printer beyond its model and
its load. The engine is the textbook list-scheduling makespan the queue page
used to compute in the browser; these tests pin the rules the spec names.
"""

from datetime import datetime, timedelta

from backend.app.services.farm_forecast import (
    FarmSnapshot,
    MachineState,
    QueuedRow,
    StagedJob,
    StaggerPolicy,
    _initial_state,
    forecast_orders,
    simulate_farm,
)
from backend.app.services.plan_engine import LinePlan, OrderPlan, PlanAlternative, PlanRow
from backend.app.services.stagger_groups import StaggerGroupResolver, StaggerSplit

NOW = datetime(2026, 9, 6, 12, 0, 0)
H = 3600


def _machine(pid, model="P1S", running=0, queued=(), accepts=True, gap=0.0, waiting=0.0, interval=None):
    return MachineState(
        printer_id=pid,
        model=model,
        running_seconds=running,
        queued=[QueuedRow(*q) for q in queued],
        accepts_new_work=accepts,
        plate_clear_seconds=gap,
        waiting_seconds=waiting,
        stagger_interval_seconds=interval,
    )


def _stagger(concurrent=1, interval=300, wait_for_bed=False, resolver=None, live=None):
    return StaggerPolicy(
        concurrent=concurrent,
        interval_seconds=interval,
        wait_for_bed=wait_for_bed,
        resolver=resolver or StaggerGroupResolver.global_only(),
        live=dict(live or {}),
    )


def _tag_resolver(tags_by_printer, tag_limits=None):
    """Two picked tags, A=10 and B=20; a printer with neither is a wildcard."""
    return StaggerGroupResolver(
        StaggerSplit(by_tags=True, tag_ids=frozenset({10, 20}), tag_limits=tag_limits or {}),
        tags_by_printer={pid: frozenset(tags) for pid, tags in tags_by_printer.items()},
        tag_names={10: "A", 20: "B"},
        location_by_printer={},
        parent_by_location={},
        location_names={},
    )


def _plan(order_id, rows):
    """One line whose rows are ``(plate_id, count, seconds, model, alternatives)``,
    an alternative being ``(plate_id, model, seconds)``."""
    plan_rows = []
    for plate_id, count, seconds, model, alts in rows:
        plan_rows.append(
            PlanRow(
                plate_id=plate_id,
                library_file_id=plate_id,
                plate_index=0,
                filename=f"f{plate_id}",
                count=count,
                print_time_seconds=seconds,
                time_unknown=seconds is None,
                printer_model=model,
                alternatives=[
                    PlanAlternative(
                        plate_id=a_id,
                        library_file_id=a_id,
                        plate_index=0,
                        filename=f"f{a_id}",
                        printer_model=a_model,
                        print_time_seconds=a_secs,
                        time_unknown=a_secs is None,
                    )
                    for a_id, a_model, a_secs in alts
                ],
            )
        )
    return OrderPlan(lines=[LinePlan(line_id=order_id * 10, product_id=1, material=None, rows=plan_rows)])


def _one(snapshot, plan, order_id=7, ordered=None):
    return forecast_orders(snapshot, {order_id: plan}, ordered or [order_id], {order_id}, NOW)[order_id]


def test_one_printer_runs_the_jobs_serially():
    f = _one(FarmSnapshot(printers=[_machine(1)], staged=[]), _plan(7, [(100, 3, H, "P1S", [])]))
    assert f.now_seconds == 3 * H and f.now_eta == NOW + timedelta(hours=3)
    assert f.machine_seconds == 3 * H and f.unknown_prints == 0 and f.unroutable_prints == 0
    assert f.lines[0].now_seconds == 3 * H and f.lines[0].line_id == 70


def test_two_printers_of_the_model_halve_the_finish():
    f = _one(FarmSnapshot(printers=[_machine(1), _machine(2)], staged=[]), _plan(7, [(100, 4, H, "P1S", [])]))
    assert f.now_seconds == 2 * H


def test_a_busy_printer_and_its_queue_delay_the_start():
    snap = FarmSnapshot(printers=[_machine(1, running=H, queued=[(None, H)])], staged=[])
    assert _one(snap, _plan(7, [(100, 1, H, "P1S", [])])).now_seconds == 3 * H


def test_alternatives_spread_the_line_across_models_and_propose_the_split():
    """Four idle P1S, one idle X1C, 100 one-hour prints: 80 land on the P1S file, 20 on the X1C file."""
    printers = [_machine(i, "P1S") for i in range(1, 5)] + [_machine(9, "X1C")]
    f = _one(FarmSnapshot(printers=printers, staged=[]), _plan(7, [(100, 100, H, "P1S", [(200, "X1C", H)])]))
    row = f.lines[0].rows[0]
    assert row.plate_id == 100 and row.proposed_split == {100: 80, 200: 20}
    assert f.now_seconds == 20 * H


def test_a_busy_machine_shifts_the_split():
    printers = [_machine(1, "P1S", running=10 * H), _machine(9, "X1C")]
    f = _one(FarmSnapshot(printers=printers, staged=[]), _plan(7, [(100, 10, H, "P1S", [(200, "X1C", H)])]))
    assert f.lines[0].rows[0].proposed_split == {100: 0, 200: 10}


def test_a_row_without_alternatives_proposes_nothing():
    f = _one(FarmSnapshot(printers=[_machine(1)], staged=[]), _plan(7, [(100, 2, H, "P1S", [])]))
    assert f.lines[0].rows[0].proposed_split is None


def test_two_rows_of_one_line_keep_their_own_proposals():
    """Row A (plate 100, alt 200) and row B (plate 200, alt 100) on the same line:
    each proposal is counted from ITS prints, not from the line's."""
    printers = [_machine(1, "P1S"), _machine(9, "X1C")]
    plan = _plan(7, [(100, 2, H, "P1S", [(200, "X1C", H)]), (200, 2, H, "X1C", [(100, "P1S", H)])])
    f = _one(FarmSnapshot(printers=printers, staged=[]), plan)
    a, b = f.lines[0].rows
    assert sum(a.proposed_split.values()) == 2 and sum(b.proposed_split.values()) == 2


def test_staged_auto_queue_work_occupies_its_model_first():
    snap = FarmSnapshot(printers=[_machine(1)], staged=[StagedJob(order_id=None, target_model="P1S", seconds=2 * H)])
    assert _one(snap, _plan(7, [(100, 1, H, "P1S", [])])).now_seconds == 3 * H


def test_after_places_the_order_ahead_first_and_now_does_not():
    snap = FarmSnapshot(printers=[_machine(1)], staged=[])
    plans = {1: _plan(1, [(100, 2, H, "P1S", [])]), 7: _plan(7, [(300, 1, H, "P1S", [])])}
    out = forecast_orders(snap, plans, [1, 7], {7}, NOW)
    assert list(out) == [7]
    f = out[7]
    assert f.now_seconds == H and f.after_seconds == 3 * H and f.ahead_count == 1
    assert f.lines[0].after_seconds == 3 * H


def test_an_order_ahead_without_a_plan_advances_nothing():
    f = _one(FarmSnapshot(printers=[_machine(1)], staged=[]), _plan(7, [(100, 1, H, "P1S", [])]), ordered=[1, 7])
    assert f.after_seconds == H and f.ahead_count == 1


def test_unknown_estimates_are_counted_not_defaulted():
    f = _one(FarmSnapshot(printers=[_machine(1)], staged=[]), _plan(7, [(100, 3, None, "P1S", [])]))
    assert f.unknown_prints == 3 and f.now_eta is None and f.now_seconds is None and f.machine_seconds is None
    assert f.lines[0].unknown_prints == 3


def test_a_model_with_no_printer_is_unroutable():
    f = _one(FarmSnapshot(printers=[_machine(1, "A1MINI")], staged=[]), _plan(7, [(100, 2, H, "P1S", [])]))
    assert f.unroutable_prints == 2 and f.now_eta is None and f.lines[0].unroutable_prints == 2
    # No model on the file at all is unroutable too — the auto-queue would not route it either.
    f = _one(FarmSnapshot(printers=[_machine(1)], staged=[]), _plan(7, [(100, 1, H, None, [])]))
    assert f.unroutable_prints == 1


def test_queued_rows_of_the_order_give_an_eta_without_a_plan():
    snap = FarmSnapshot(printers=[_machine(1, queued=[(7, H), (7, None)])], staged=[])
    f = forecast_orders(snap, {}, [7], {7}, NOW)[7]
    assert f.now_seconds == H and f.machine_seconds == 0 and f.unknown_prints == 1 and f.lines == []


def test_the_farm_free_at_is_the_last_printer():
    snap = FarmSnapshot(
        printers=[_machine(1, running=H), _machine(2, "X1C", queued=[(None, 2 * H)])],
        staged=[StagedJob(order_id=None, target_model="X1C", seconds=H)],
    )
    assert simulate_farm(snap).free_seconds == 3 * H


def test_every_printer_carries_its_own_free_at():
    """The «free at» sorts order by this. Printer 1 owes its running hour;
    printer 2 owes its queued two hours plus the staged X1C hour the
    simulation deals to it; printer 3 holds a row with no estimate, so its
    number is zero AND its counter says why — an idle machine and a busy one
    with no estimate must not read alike."""
    snap = FarmSnapshot(
        printers=[
            _machine(1, running=H),
            _machine(2, "X1C", queued=[(None, 2 * H)]),
            _machine(3, queued=[(None, None)]),
        ],
        staged=[StagedJob(order_id=None, target_model="X1C", seconds=H)],
    )
    farm = simulate_farm(snap)
    rows = {p.printer_id: (p.free_seconds, p.unknown_prints) for p in farm.printers}
    assert rows == {1: (H, 0), 2: (3 * H, 0), 3: (0, 1)}
    # The farm's own number is the last of them, and its counter the sum of theirs.
    assert farm.free_seconds == max(secs for secs, _ in rows.values())
    assert farm.unknown_prints == sum(unknown for _, unknown in rows.values())


def test_models_match_after_normalisation():
    snap = FarmSnapshot(printers=[_machine(1, "Bambu Lab P1S")], staged=[])
    f = _one(snap, _plan(7, [(100, 1, H, "P1S", [])]))
    assert f.unroutable_prints == 0 and f.now_seconds == H


def test_a_row_the_farm_cannot_place_proposes_nothing():
    """No printer of either model: the counters say why, and there is no split to apply."""
    f = _one(
        FarmSnapshot(printers=[_machine(1, "A1MINI")], staged=[]), _plan(7, [(100, 3, H, "P1S", [(200, "X1C", H)])])
    )
    assert f.unroutable_prints == 3 and f.lines[0].rows[0].proposed_split is None


def test_an_overrun_print_advances_nothing_and_is_not_unknown():
    snap = FarmSnapshot(printers=[_machine(1, queued=[(7, 0), (7, H)])], staged=[])
    f = forecast_orders(snap, {}, [7], {7}, NOW)[7]
    assert f.unknown_prints == 0 and f.now_seconds == H


def test_a_parked_printer_finishes_what_it_holds_and_takes_nothing_new():
    """Availability decides who RECEIVES work, never what a machine OWES.

    Printer 1 is parked (maintenance / a paused queue) with one of order 7's
    hours already on it. That hour still dates the order and still counts in
    the farm's «free at» — but all three of the order's new prints go to the
    healthy printer 2 and run serially there (3 h); handing the parked machine
    its share would read 2 h, promising a date no operator will meet.
    """
    printers = [_machine(1, accepts=False, queued=[(7, H)]), _machine(2)]
    f = _one(FarmSnapshot(printers=printers, staged=[]), _plan(7, [(100, 3, H, "P1S", [])]))
    assert f.now_seconds == 3 * H
    # Nobody but the parked printer owes anything: its hour IS the farm's «free at».
    assert simulate_farm(FarmSnapshot(printers=printers, staged=[])).free_seconds == H


def test_the_farm_counts_its_estimate_less_rows():
    """The queue tile's «why»: rows with no estimate, whoever they belong to."""
    snap = FarmSnapshot(
        printers=[_machine(1, queued=[(None, None)])],
        staged=[StagedJob(order_id=None, target_model="P1S", seconds=None)],
    )
    assert simulate_farm(snap).unknown_prints == 2


def test_a_row_whose_own_model_is_absent_routes_to_its_alternative():
    """One P1S in the farm and a row sliced for an X1C: the alternative carries
    the whole row — nothing is unroutable, and the split says so."""
    f = _one(
        FarmSnapshot(printers=[_machine(1, "P1S")], staged=[]),
        _plan(7, [(100, 2, H, "X1C", [(200, "P1S", H)])]),
    )
    assert f.unroutable_prints == 0
    assert f.lines[0].rows[0].proposed_split == {100: 0, 200: 2}
    assert f.now_seconds == 2 * H


# ---------- v2: preparation, plate clear, drying (spec §4, §5) ----------


def test_prep_is_paid_before_every_print_that_has_not_started():
    """The running head pays nothing; each planned print pays the allowance before it starts."""
    snap = FarmSnapshot(printers=[_machine(1, running=H)], staged=[], prep_seconds=600)
    assert _one(snap, _plan(7, [(100, 2, H, "P1S", [])])).now_seconds == 3 * H + 1200


def test_a_queued_row_pays_its_own_prep():
    snap = FarmSnapshot(printers=[_machine(1, queued=[(None, H, 300)])], staged=[], prep_seconds=600)
    assert _one(snap, _plan(7, [(100, 1, H, "P1S", [])])).now_seconds == 2 * H + 900


def test_a_plate_clear_gap_follows_every_print_on_a_gated_printer():
    gated = FarmSnapshot(printers=[_machine(1, running=H, gap=600)], staged=[])
    assert _one(gated, _plan(7, [(100, 1, H, "P1S", [])])).now_seconds == 2 * H + 600
    # The machine is free only once its LAST plate is cleared too: the head, then the gap.
    assert simulate_farm(gated).printers[0].free_seconds == H + 600
    plain = FarmSnapshot(printers=[_machine(1, running=H)], staged=[])
    assert _one(plain, _plan(7, [(100, 1, H, "P1S", [])])).now_seconds == 2 * H


def test_a_printer_awaiting_its_plate_waits_the_gap_before_its_first_print():
    snap = FarmSnapshot(printers=[_machine(1, waiting=600)], staged=[])
    assert _one(snap, _plan(7, [(100, 1, H, "P1S", [])])).now_seconds == H + 600
    assert simulate_farm(snap).free_seconds == 600


def test_a_blocking_drying_cycle_holds_the_printer_no_longer_than_its_head():
    idle = FarmSnapshot(printers=[_machine(1, waiting=1800)], staged=[])
    assert _one(idle, _plan(7, [(100, 1, H, "P1S", [])])).now_seconds == H + 1800
    # A cycle that ends before the running print does adds nothing.
    busy = FarmSnapshot(printers=[_machine(1, running=H, waiting=1800)], staged=[])
    assert _one(busy, _plan(7, [(100, 1, H, "P1S", [])])).now_seconds == 2 * H


def test_assumptions_travel_from_the_snapshot_to_the_order():
    plan = _plan(7, [(100, 1, H, "P1S", [])])
    assert _one(FarmSnapshot(printers=[_machine(1)], staged=[]), plan).assumptions == []
    drying = FarmSnapshot(printers=[_machine(1)], staged=[], assumptions=("drying",))
    assert _one(drying, plan).assumptions == ["drying"]


# ---------- v2: staggered start (spec §5.2) ----------


def test_stagger_serialises_starts_by_cap_and_interval():
    """Four idle P1S, cap 2, interval 5 min, four one-hour prints: two start now, two five minutes later."""
    printers = [_machine(i) for i in range(1, 5)]
    plan = _plan(7, [(100, 4, H, "P1S", [])])
    staggered = FarmSnapshot(printers=printers, staged=[], stagger=_stagger(concurrent=2, interval=300))
    assert _one(staggered, plan).now_seconds == H + 300
    assert _one(FarmSnapshot(printers=printers, staged=[]), plan).now_seconds == H


def test_stagger_groups_cap_each_phase_alone():
    resolver = _tag_resolver({1: {10}, 2: {10}, 3: {20}, 4: {20}})
    printers = [_machine(i) for i in range(1, 5)]
    plan = _plan(7, [(100, 4, H, "P1S", [])])
    by_group = FarmSnapshot(
        printers=printers, staged=[], stagger=_stagger(concurrent=1, interval=300, resolver=resolver)
    )
    assert _one(by_group, plan).now_seconds == H + 300
    farm_wide = FarmSnapshot(printers=printers, staged=[], stagger=_stagger(concurrent=1, interval=300))
    assert _one(farm_wide, plan).now_seconds == H + 900


def test_forecast_uses_raised_override_but_other_group_inherits_default():
    plan = _plan(7, [(100, 6, H, "P1S", [])])
    printers = [_machine(i) for i in range(1, 7)]
    for tag, expected in ((10, H), (20, H + 600)):
        resolver = _tag_resolver({i: {tag} for i in range(1, 7)}, tag_limits={10: 6})
        snap = FarmSnapshot(
            printers=printers, staged=[], stagger=_stagger(concurrent=2, interval=300, resolver=resolver)
        )
        assert _one(snap, plan).now_seconds == expected


def test_a_wildcard_printer_waits_for_room_in_every_group():
    """Printer 1 (tag A) is heating for another 5 min; untagged printer 5 is in A and B, so it waits."""
    plan = _plan(7, [(100, 1, H, "P1S", [])])
    wild = _stagger(concurrent=1, interval=300, resolver=_tag_resolver({1: {10}}), live={1: 300})
    assert _one(FarmSnapshot(printers=[_machine(5)], staged=[], stagger=wild), plan).now_seconds == H + 300
    tagged_b = _stagger(concurrent=1, interval=300, resolver=_tag_resolver({1: {10}, 5: {20}}), live={1: 300})
    assert _one(FarmSnapshot(printers=[_machine(5)], staged=[], stagger=tagged_b), plan).now_seconds == H


def test_live_slots_seed_the_ledger():
    """A slot the scheduler holds right now - on a printer the snapshot may not even list - counts."""
    snap = FarmSnapshot(printers=[_machine(1)], staged=[], stagger=_stagger(concurrent=1, interval=300, live={9: 200}))
    assert _one(snap, _plan(7, [(100, 1, H, "P1S", [])])).now_seconds == H + 200


def test_a_printer_never_blocks_itself():
    """Two one-minute prints on one printer, cap 1, interval 5 min: the second starts when the first ends."""
    snap = FarmSnapshot(printers=[_machine(1)], staged=[], stagger=_stagger(concurrent=1, interval=300))
    assert _one(snap, _plan(7, [(100, 2, 60, "P1S", [])])).now_seconds == 120


def test_wait_for_bed_holds_the_slot_through_prep():
    printers = [_machine(1), _machine(2)]
    plan = _plan(7, [(100, 2, H, "P1S", [])])
    held = FarmSnapshot(printers=printers, staged=[], prep_seconds=600, stagger=_stagger(1, 300, wait_for_bed=True))
    assert _one(held, plan).now_seconds == H + 1500
    freed = FarmSnapshot(printers=printers, staged=[], prep_seconds=600, stagger=_stagger(1, 300, wait_for_bed=False))
    assert _one(freed, plan).now_seconds == H + 900


def test_a_printers_own_interval_beats_the_farm_default():
    printers = [_machine(1), _machine(2, interval=600), _machine(3)]
    snap = FarmSnapshot(printers=printers, staged=[], stagger=_stagger(concurrent=1, interval=300))
    assert _one(snap, _plan(7, [(100, 3, H, "P1S", [])])).now_seconds == H + 900


def test_the_running_head_takes_no_slot_of_its_own():
    """The head is past dispatch: only a LIVE slot (from the policy) can hold others back for it."""
    printers = [_machine(1, running=H), _machine(2)]
    snap = FarmSnapshot(printers=printers, staged=[], stagger=_stagger(concurrent=1, interval=300))
    assert _one(snap, _plan(7, [(100, 1, H, "P1S", [])])).now_seconds == H


def test_the_routing_clock_is_the_sequenced_finish():
    """After the walk, ``free_at`` is when the machine is really free - what the next order routes against."""
    printers = [_machine(1, queued=[(None, H)]), _machine(2, queued=[(None, H)])]
    state = _initial_state(FarmSnapshot(printers=printers, staged=[], stagger=_stagger(concurrent=1, interval=300)))
    assert [m.free_at for m in state.machines] == [H, H + 300]
