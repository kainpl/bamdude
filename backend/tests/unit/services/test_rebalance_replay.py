"""The router, replayed (spec rebalance-tail-time r2) — pure, every number worked out by hand.

A lane is a printer; a job is a pending auto-queue row with the printers it fits. Unless a test
says so there is no preparation, no plate service and no stagger, so a print on a printer free at
t ends at t + its seconds.
"""

import random

import pytest

from backend.app.services.farm_forecast import StaggerPolicy
from backend.app.services.rebalance_replay import (
    Held,
    Job,
    Lane,
    ReplayBudget,
    components,
    jumps,
    replay,
    router_key,
)
from backend.app.services.stagger_groups import StaggerGroupResolver, StaggerSplit
from backend.tests.unit.services._replay_reference import reference_replay


def _job(job_id, *pids, seconds=100.0, position=0, group="P1S", jumped=False, exact=True, colour=0, prep=0.0):
    return Job(
        job_id=job_id,
        position=position,
        group=group,
        seconds=seconds,
        fits=tuple((pid, exact, colour) for pid in pids),
        been_jumped=jumped,
        prep=prep,
    )


def _busy(pid, seconds, **kw):
    return Lane(pid, held=(Held(seconds, started=True),), **kw)


def _free(pid, ready=True, **kw):
    return Lane(pid, ready=ready, **kw)


def _one_bucket(concurrent=1, interval=600, wait_for_bed=False, live=None):
    return StaggerPolicy(
        concurrent=concurrent,
        interval_seconds=interval,
        wait_for_bed=wait_for_bed,
        resolver=StaggerGroupResolver.global_only(),
        live=dict(live or {}),
    )


# ---------- the tail ----------


def test_a_job_that_stays_home_holds_its_printer_for_the_next_one():
    """T-A01: free in 60 s, four 100 s jobs — 160, 260, 360, 460."""
    result = replay([_busy(1, 60)], [_job(i, 1, position=i) for i in (1, 2, 3, 4)])
    assert [result.finish[i] for i in (1, 2, 3, 4)] == [160, 260, 360, 460]


def test_a_head_no_printer_can_take_does_not_block_the_next_job():
    """T-A09: the first job fits only a busy printer; the second, behind it, takes the free one now."""
    result = replay([_busy(1, 1000), _free(2)], [_job(1, 1, position=1), _job(2, 2, position=2)])
    assert result.finish == {1: 1100, 2: 100}
    assert result.routed_at == {1: 1000, 2: 0}


def test_a_job_no_printer_fits_is_never_routed():
    result = replay([_free(1)], [_job(1), _job(2, 1)])
    assert result.outcome(1) == ("none", None)
    assert result.outcome(2) == ("known", 100)


def test_the_router_ranks_ready_then_exact_then_colour_then_the_lower_id():
    """find_eligible_printer's max((ready, exact, colour, -id)) among the free printers it fits."""
    lanes = [_free(1, ready=False), _free(2), _free(3), _free(4)]

    def chosen(job):
        return replay(lanes, [job]).lane_of[job.job_id]

    assert chosen(Job(1, 0, "P1S", 100.0, fits=((1, True, 9), (2, True, 0)))) == 2  # ready beats colour
    assert chosen(Job(1, 0, "P1S", 100.0, fits=((2, False, 9), (3, True, 0)))) == 3  # exact beats colour
    assert chosen(Job(1, 0, "P1S", 100.0, fits=((2, True, 1), (3, True, 2)))) == 3  # more colour matches
    assert chosen(Job(1, 0, "P1S", 100.0, fits=((3, True, 0), (2, True, 0)))) == 2  # the lower id


def test_a_printer_awaiting_its_plate_is_routed_but_starts_when_served():
    """Routing is not dispatching: a free printer awaiting its plate clear takes the job when no
    ready one fits, and the print starts after what holds it (300 s)."""
    result = replay([_free(1, ready=False, waiting=300)], [_job(1, 1)])
    assert result.routed_at[1] == 0 and result.finish[1] == 400
    assert result.ready_when_routed[1] is False


# ---------- the router's order ----------


def test_the_order_is_the_position_then_the_id():
    """T-A06: two jobs at one position — the lower id goes first; the position beats the id."""
    result = replay([_free(1)], [_job(7, 1, position=5), _job(3, 1, position=5), _job(9, 1, position=2)])
    assert [result.finish[i] for i in (9, 3, 7)] == [100, 200, 300]
    assert router_key(_job(3, position=5), sjf=False) < router_key(_job(7, position=5), sjf=False)


def test_sjf_orders_by_model_then_jumped_then_time_then_position():
    """SJF: the model column first (NULL first), been_jumped DESC, the time (no estimate last)."""
    jobs = [
        _job(1, 1, seconds=300, group="X1C", position=1),
        _job(2, 1, seconds=300, group="A1", position=9),
        _job(3, 1, seconds=None, group="A1", position=2),
        _job(4, 1, seconds=500, group="A1", position=3, jumped=True),
        _job(5, 1, seconds=200, group=None, position=8),
    ]
    order = sorted(jobs, key=lambda j: router_key(j, sjf=True))
    assert [j.job_id for j in order] == [5, 4, 2, 3, 1]


def test_a_mark_reorders_the_next_wave_and_never_the_open_one():
    """T-A07: SJF. A 100 s job at position 3 goes first; placing it marks the 500 s job at
    position 1 (earlier and longer) been_jumped, but not the 300 s job at position 5 (later).
    On one printer the marked job goes ahead of the shorter one at 100 s — plain SJF would
    have taken the 300 s job. On two printers the open wave keeps its order: the 300 s job
    takes the second printer at 0 and the marked one waits for the first."""
    jobs = [
        _job(1, 1, 2, seconds=500, position=1),
        _job(2, 1, 2, seconds=300, position=5),
        _job(3, 1, 2, seconds=100, position=3),
    ]
    result = replay([_free(1)], jobs, sjf=True)
    assert result.finish == {3: 100, 1: 600, 2: 900}
    result = replay([_free(1), _free(2)], jobs, sjf=True)
    assert result.finish == {3: 100, 2: 300, 1: 600}
    assert result.lane_of == {3: 1, 2: 2, 1: 1}


def test_a_zero_estimate_sorts_and_marks_as_the_router_reads_it():
    """Codex r1 T-R2: the router orders SJF by the stored time, NULL last — a stored 0 comes
    before 100. Its duration is unknown (T19), its place is not: A (0, position 2) goes first, and
    placing it marks B (100, ahead of it) as jumped, as `_mark_jumped_peers` does with 0."""
    a = Job(1, 2, "P1S", None, fits=((1, True, 0),), order_seconds=0.0)
    b = _job(2, 1, seconds=100, position=1)
    for result in (replay([_free(1)], [a, b], sjf=True), reference_replay([_free(1)], [a, b], sjf=True)):
        assert result.routed_at[1] == 0 and 2 not in result.routed_at or result.routed_at[2] > 0
        assert result.lane_of[1] == 1
    assert jumps(a, b, peer_jumped=False)
    assert router_key(a, sjf=True) < router_key(b, sjf=True)


def test_a_job_with_no_model_sorts_where_the_database_sorts_null():
    """Codex r1 T-R2: `ORDER BY target_model` puts NULL first on SQLite and last on
    PostgreSQL; the router's SQL is not changed, so the replay follows the database it reads."""
    blank = Job(1, 0, None, 100.0, fits=((1, True, 0),))
    named = Job(2, 0, "P1S", 100.0, fits=((1, True, 0),))
    assert router_key(blank, sjf=True) < router_key(named, sjf=True)
    assert router_key(blank, sjf=True, null_group_first=False) > router_key(named, sjf=True, null_group_first=False)
    lanes = [_free(1)]
    for null_first, first in ((True, 1), (False, 2)):
        for result in (
            replay(lanes, [blank, named], sjf=True, null_group_first=null_first),
            reference_replay(lanes, [blank, named], sjf=True, null_group_first=null_first),
        ):
            assert result.routed_at[first] == 0


def test_a_mark_inside_a_wave_leaves_that_wave_in_its_order():
    """T10: printers 1 and 2 free together. The wave's order is J2 (40 s), J3 (100 s), J1
    (250 s). Placing J2 marks J1 (earlier, longer) as jumped — for the NEXT wave: printer 2
    still goes to J3, the next in the order this wave was sorted in, as the router's pass keeps
    the list it fetched; J1 waits although its mark now outranks J3."""
    lanes = [_busy(1, 100), _busy(2, 100)]
    jobs = [
        _job(1, 2, seconds=250, position=4),
        _job(2, 1, seconds=40, position=5),
        _job(3, 1, 2, seconds=100, position=5),
    ]
    for result in (replay(lanes, jobs, sjf=True), reference_replay(lanes, jobs, sjf=True)):
        assert (result.lane_of[2], result.lane_of[3]) == (1, 2)
        assert result.routed_at[3] == 100 and result.routed_at[1] == 200


def test_a_mark_needs_the_same_model_an_earlier_position_and_a_longer_or_unknown_time():
    started = _job(10, seconds=100, position=5)
    assert jumps(started, _job(1, seconds=None, position=1), peer_jumped=False)
    assert not jumps(started, _job(1, seconds=100, position=1), peer_jumped=False)  # not longer
    assert not jumps(started, _job(1, seconds=500, position=9), peer_jumped=False)  # later position
    assert not jumps(started, _job(1, seconds=500, position=1, group="X1C"), peer_jumped=False)  # other model
    assert not jumps(_job(10, seconds=None, position=5), _job(1, seconds=500, position=1), peer_jumped=False)


# ---------- the time rules ----------


def test_the_plate_service_and_the_preparation_and_auto_eject():
    """T-A13: a 60 s gap after each print unless auto-ejected; a not-yet-started print pays 20 s prep;
    a running print pays no prep and is followed by its gap."""
    lane = Lane(1, held=(Held(100, started=True),), gap=60)
    result = replay([lane], [_job(1, 1, prep=20), Job(2, 1, "P1S", 100.0, fits=((1, True, 0),), prep=20)])
    # head ends 100, gap to 160; job 1: 160 + 20 + 100 = 280, gap to 340; job 2 ends 340 + 120 = 460
    assert result.finish == {1: 280, 2: 460}
    ejected = Lane(1, held=(Held(100, started=True),), gap=60)
    result = replay([ejected], [Job(1, 0, "P1S", 100.0, fits=((1, True, 0),), auto_eject=True), _job(2, 1, position=1)])
    # job 1 starts after the head's gap (160), ends 260, no gap: job 2 ends 360
    assert result.finish == {1: 260, 2: 360}


def test_the_operator_serves_the_plate_in_their_hours():
    class Hours:
        def service_finish(self, finish, duration):
            return max(finish, 1000) + duration  # nobody is here before 1000 s

    lane = Lane(1, held=(Held(100, started=True),), gap=50, requires_operator=True)
    result = replay([lane], [_job(1, 1)], operator_hours=Hours())
    assert result.finish == {1: 1150}


def test_a_staggered_start_dates_the_second_print():
    """T-A11: one start per 600 s window for the farm — the second of two 100 s prints ends at 700."""
    result = replay([_free(2), _free(3)], [_job(1, 2, 3, position=1), _job(2, 2, 3, position=2)], stagger=_one_bucket())
    assert sorted(result.finish.values()) == [100, 700]


def test_a_running_print_opens_no_window_and_a_live_slot_holds_one():
    """The head is already started; a slot the scheduler holds now delays the first start."""
    result = replay([_busy(1, 50), _free(2)], [_job(1, 2)], stagger=_one_bucket(live={9: 400}))
    assert result.finish == {1: 500}


def test_wait_for_bed_and_a_printers_own_interval():
    """With wait-for-bed the window covers the preparation; a printer's own interval overrides."""
    lanes = [_free(1, interval=1000), _free(2)]
    jobs = [_job(1, 1, position=1, prep=50), _job(2, 2, position=2, prep=50)]
    result = replay(lanes, jobs, stagger=_one_bucket(wait_for_bed=True))
    # job 1 on printer 1 opens [0, 50 + 1000); job 2 waits until 1050, ends 1050 + 50 + 100
    assert result.finish == {1: 150, 2: 1200}


def test_separate_stagger_groups_do_not_wait_for_each_other():
    """T-A12: printers in different groups start together; a printer in both waits for both."""
    split = StaggerSplit(by_tags=True, tag_ids=frozenset({10, 20}))
    resolver = StaggerGroupResolver(
        split,
        tags_by_printer={1: frozenset({10}), 2: frozenset({20}), 3: frozenset({10, 20})},
        tag_names={10: "A", 20: "B"},
        location_by_printer={},
        parent_by_location={},
        location_names={},
    )
    stagger = StaggerPolicy(concurrent=1, interval_seconds=600, wait_for_bed=False, resolver=resolver)
    result = replay([_free(1), _free(2)], [_job(1, 1, position=1), _job(2, 2, position=2)], stagger=stagger)
    assert result.finish == {1: 100, 2: 100}
    result = replay([_free(1), _free(3)], [_job(1, 1, position=1), _job(2, 3, position=2)], stagger=stagger)
    assert result.finish == {1: 100, 2: 700}


# ---------- what is not known ----------


def test_a_job_without_an_estimate_ends_unknown_and_so_does_what_could_have_used_its_printer():
    """T-A10: printer 1 takes a job of no estimate — its future is unknown. A later job that
    could print on printer 1 might have; one that fits only printer 2 is still known."""
    jobs = [_job(1, 1, seconds=None, position=1), _job(2, 1, 2, position=2), _job(3, 2, position=3)]
    result = replay([_free(1), _free(2)], jobs)
    assert result.outcome(1) == ("unknown", None)
    assert result.outcome(2) == ("known", 100)  # routed at 0, before printer 1's future was unknown
    assert result.outcome(3) == ("known", 200)
    jobs = [_job(1, 1, seconds=None, position=1), _job(2, 2, position=2), _job(3, 1, 2, position=3)]
    result = replay([_free(1), _free(2)], jobs)
    assert result.outcome(3) == ("unknown", None)  # routed at 100, printer 1 might have been free


def test_a_ready_printer_whose_queue_will_not_start_ranks_as_ready_now():
    """The router ranks a free printer by the pass's fact alone: one whose queue is in error is
    ready all the same and wins on colour — the job waits there, its start unknown (T-A25)."""
    lanes = [_free(1, ready=True, hold_unknown=True), _free(2, ready=True)]
    job = Job(1, 0, "P1S", 100.0, fits=((1, True, 1), (2, True, 0)))
    for result in (replay(lanes, [job]), reference_replay(lanes, [job])):
        assert result.lane_of[1] == 1 and result.ready_when_routed[1] is True
        assert result.outcome(1) == ("unknown", None)


def test_a_printer_held_with_no_release_makes_its_job_unknown_never_zero():
    """An HA interlock: free for routing, but when it starts nobody knows."""
    result = replay([_free(1, ready=False, hold_unknown=True)], [_job(1, 1)])
    assert result.outcome(1) == ("unknown", None)


def test_a_running_print_without_an_estimate_is_unknown_and_reaches_its_stagger_group():
    result = replay([_busy(1, None), _free(2)], [_job(1, 1, position=1), _job(2, 2, position=2)])
    assert result.outcome(1) == ("unknown", None) and result.outcome(2) == ("known", 100)
    result = replay([_busy(1, None), _free(2)], [_job(1, 1, position=1), _job(2, 2, position=2)], stagger=_one_bucket())
    assert result.outcome(2) == ("unknown", None)


def test_a_printer_no_job_can_reach_taints_no_start_group():
    """Final review C1: printer 1 runs a print with no estimate, and no pending job fits it, so
    nothing new can start there before the forecast's end — its start group stays known. J (lane
    2) starts at once, K follows on the same printer (its own window never blocks it). With a job that could reach
    printer 1, its unknown release reaches the group, and K is unknown."""
    lanes = [_busy(1, None), _free(2)]
    jobs = [_job(1, 2, seconds=100, position=1), _job(2, 2, seconds=100, position=2)]
    for result in (replay(lanes, jobs, stagger=_one_bucket()), reference_replay(lanes, jobs, stagger=_one_bucket())):
        assert result.outcome(1) == ("known", 100) and result.outcome(2) == ("known", 200)
    reach = [*jobs, _job(3, 1, 2, seconds=100, position=3)]
    for result in (replay(lanes, reach, stagger=_one_bucket()), reference_replay(lanes, reach, stagger=_one_bucket())):
        assert result.outcome(2) == ("unknown", None)


def test_a_queue_that_starts_nothing_holds_its_job_and_nobody_else():
    """Final review I1: a queue in error starts nothing, so the job the router places there
    waits for the operator (no finish, as no home) and the printer never frees in the forecast — but it takes
    no start window and leaves every other job known. A row an order forbids on a busy printer
    is the same: the printer never frees."""
    lanes = [Lane(1, ready=True, stuck=True), _free(2), Lane(3, held=(Held(None, blocked=True),))]
    jobs = [
        _job(1, 1, 2, seconds=100, position=1, colour=0),
        _job(2, 2, 3, seconds=100, position=2),
        _job(3, 1, 2, 3, seconds=100, position=3),
    ]
    for result in (replay(lanes, jobs, stagger=_one_bucket()), reference_replay(lanes, jobs, stagger=_one_bucket())):
        assert result.lane_of[1] == 1 and result.outcome(1) == ("none", None)
        assert result.outcome(2) == ("known", 100)
        assert result.lane_of[3] == 2 and result.outcome(3) == ("known", 200)


def test_the_budget_is_a_refusal_never_a_partial_answer():
    with pytest.raises(ReplayBudget):
        replay([_free(1)], [_job(i, 1, position=i) for i in range(50)], budget=20)


# ---------- the fast replay against the reference, and its parts against the whole ----------


def _random_farm(rng):
    lanes = []
    for pid in range(1, rng.randint(1, 5) + 1):
        kind = rng.random()
        if kind < 0.35:
            lanes.append(_busy(pid, rng.choice([None, 50.0, 120.0, 400.0]) if rng.random() < 0.9 else 300.0))
        elif kind < 0.45:
            lanes.append(Lane(pid, held=(Held(200.0, prep=10.0),), waiting=rng.choice([0.0, 30.0])))
        elif kind < 0.5:
            # Busy for good: a running print, then a row an order forbids here.
            lanes.append(Lane(pid, held=(Held(150.0, started=True), Held(None, blocked=True))))
        else:
            lanes.append(
                Lane(
                    pid,
                    ready=rng.random() < 0.7,
                    waiting=rng.choice([0.0, 0.0, 90.0]),
                    hold_unknown=rng.random() < 0.08,
                    stuck=rng.random() < 0.08,
                    gap=rng.choice([0.0, 25.0]),
                    routable=rng.random() < 0.95,
                    interval=rng.choice([0.0, 0.0, 150.0]),
                )
            )
    jobs = []
    for job_id in range(1, rng.randint(1, 12) + 1):
        pids = [lane.printer_id for lane in lanes if rng.random() < 0.6]
        jobs.append(
            Job(
                job_id=job_id,
                position=rng.randint(0, 6),
                group=rng.choice(["A1", "P1S", "X1C", None]),
                seconds=(seconds := rng.choice([None, 40.0, 100.0, 100.0, 250.0]) if rng.random() < 0.95 else 100.0),
                # A stored 0: no duration, yet its place in the SJF order (Codex r1 T-R2).
                order_seconds=0.0 if seconds is None and rng.random() < 0.3 else None,
                fits=tuple((pid, rng.random() < 0.8, rng.randint(0, 2)) for pid in pids),
                been_jumped=rng.random() < 0.1,
                auto_eject=rng.random() < 0.2,
                prep=rng.choice([0.0, 15.0]),
            )
        )
    stagger = None
    if rng.random() < 0.5:
        stagger = _one_bucket(
            concurrent=rng.randint(1, 2),
            interval=rng.choice([60, 200]),
            wait_for_bed=rng.random() < 0.5,
            live={9: rng.choice([0, 80])} if rng.random() < 0.3 else None,
        )
    return lanes, jobs, stagger


def _as_tuple(result):
    return (result.finish, result.unknown, result.lane_of, result.routed_at, result.ready_when_routed)


@pytest.mark.parametrize("seed", range(300))
def test_the_fast_replay_matches_the_reference(seed):
    rng = random.Random(seed)
    lanes, jobs, stagger = _random_farm(rng)
    sjf = rng.random() < 0.5
    null_first = rng.random() < 0.5  # SQLite's NULL order, or PostgreSQL's
    fast = replay(lanes, jobs, sjf=sjf, stagger=stagger, null_group_first=null_first)
    slow = reference_replay(lanes, jobs, sjf=sjf, stagger=stagger, null_group_first=null_first)
    assert _as_tuple(fast) == _as_tuple(slow)


@pytest.mark.parametrize("seed", range(300))
def test_each_part_replayed_alone_ends_as_it_does_in_the_whole(seed):
    """T-A28: components are exact — a part's jobs end where the whole replay puts them."""
    rng = random.Random(1000 + seed)
    lanes, jobs, stagger = _random_farm(rng)
    sjf = rng.random() < 0.5
    whole = replay(lanes, jobs, sjf=sjf, stagger=stagger)
    by_pid = {lane.printer_id: lane for lane in lanes}
    by_id = {job.job_id: job for job in jobs}
    seen_jobs, seen_lanes = set(), set()
    for printers, members in components(lanes, jobs, sjf=sjf, stagger=stagger):
        assert not (printers & seen_lanes) and not (members & seen_jobs)
        seen_lanes |= printers
        seen_jobs |= members
        part = replay([by_pid[p] for p in printers], [by_id[j] for j in members], sjf=sjf, stagger=stagger)
        for job_id in members:
            assert part.outcome(job_id) == whole.outcome(job_id)
            assert part.lane_of.get(job_id) == whole.lane_of.get(job_id)
    assert seen_jobs == set(by_id) and seen_lanes == set(by_pid)


def test_a_placement_in_one_part_reorders_its_models_jobs_in_another():
    """T21.3 / T-A26: with SJF, placing a job marks the earlier, longer jobs of its model as
    jumped wherever they wait. K (50 s) goes to the free printer 2 at once and marks J1 (500 s,
    ahead of it) but not J3 (behind it); when printer 1 frees at 100 s, J1 goes first although
    J3 is shorter. Printers 1 and 2 share no job and no start group — the model's SJF group
    alone joins them, and a part replayed without it would put J3 first."""
    lanes = [_busy(1, 100), _free(2)]
    jobs = [
        _job(1, 1, seconds=500, position=1),
        _job(2, 2, seconds=50, position=3),
        _job(3, 1, seconds=400, position=4),
    ]
    whole = replay(lanes, jobs, sjf=True)
    assert (whole.finish[1], whole.finish[2], whole.finish[3]) == (600, 50, 1000)
    assert components(lanes, jobs, sjf=True) == [(frozenset({1, 2}), frozenset({1, 2, 3}))]
    apart = sorted(sorted(members) for _printers, members in components(lanes, jobs, sjf=False))
    assert apart == [[1, 3], [2]]


def test_a_held_printer_that_is_printing_starts_nothing_known_after_its_print():
    """An HA interlock does not stop a running print, but every start after it is unknown: the
    queued row behind the head, and a job routed to it once the head ends."""
    lane = Lane(1, held=(Held(100, started=True), Held(50)), hold_unknown=True)
    result = replay([lane, _free(2)], [_job(1, 1, position=1), _job(2, 2, position=2)])
    assert result.outcome(1) == ("unknown", None) and result.outcome(2) == ("known", 100)
    lane = Lane(1, held=(Held(100, started=True),), hold_unknown=True)
    result = replay([lane], [_job(1, 1)])
    assert result.outcome(1) == ("unknown", None)


@pytest.mark.parametrize("seed", range(300))
def test_a_replay_stopped_early_answers_what_it_watched_as_the_whole_one_does(seed):
    """T20 / T21: a trial needs only the moved prints. Stopped once every watched job has a
    known start or is unknown — or once the clock passes the home's finish — the replay gives
    each watched job the whole replay's answer, or, cut by the clock, leaves unanswered only
    jobs the whole replay ends after that time (or cannot forecast). It never does more work."""
    rng = random.Random(5000 + seed)
    lanes, jobs, stagger = _random_farm(rng)
    sjf = rng.random() < 0.5
    whole = replay(lanes, jobs, sjf=sjf, stagger=stagger)
    watch = frozenset(job.job_id for job in rng.sample(jobs, rng.randint(1, min(3, len(jobs)))))
    until = rng.choice([None, 0.0, 100.0, 250.0, 600.0])
    cut = replay(lanes, jobs, sjf=sjf, stagger=stagger, watch=watch, until=until)
    assert cut.work <= whole.work
    for job_id in watch:
        state, finish = cut.outcome(job_id)
        if state != "none" or not cut.cut:
            assert (state, finish) == whole.outcome(job_id), job_id
        else:
            assert until is not None
            full_state, full_finish = whole.outcome(job_id)
            assert full_state != "known" or full_finish > until, (job_id, full_finish, until)
