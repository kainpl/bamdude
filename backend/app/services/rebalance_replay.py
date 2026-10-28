"""How the router will deal the auto-queue out — a pure event replay (spec rebalance-tail-time r2).

The rebalancer compares two futures of one farm: a job left where it is, and the same job
replaced by another plate's prints. Both are this one function over plain values: the printers
("lanes") with what they already hold, every job the router can see, and the farm's time rules
read from ``farm_forecast`` — the preparation before a start, the plate service after a print
(with the operator's hours), and the staggered start (its own ``_admit``, asked in wall-clock
order as it requires). Nothing here reads the database, a printer or the clock.

**The router, replayed.** A printer that frees is routable at that moment (the queue counts it
busy only while it holds a running print or a queued row), so routing happens in waves at the
moments printers free. In a wave the jobs are taken in the router's order (:func:`router_key`:
position, or SJF's model / ``been_jumped`` / time / position — with the id as the last key,
which ``_fetch_pending`` carries too) and each takes the best free printer it fits, ranked as
``find_eligible_printer`` ranks: ready, exact model, colour matches, the lower id. A job that
fits no free printer does not block the next one. With SJF a placement marks its peers
``been_jumped`` (``_mark_jumped_peers``), and the next wave — never the open one — sees it.

**What is not known stays unknown, and spreads only where it can reach.** A print without an
estimate, a printer held with no forecast release (an HA interlock; a busy claim with no timed
print), make a lane's future unknown from that moment ("tainted"). A job routed while a lane it
could print on is tainted might have gone there; a job that starts on a tainted lane starts at
an unknown time; with stagger on, an unknown start makes its groups' later starts unknown; with
SJF, an uncertain job makes its model group's later order unknown. Every such job ends
``unknown`` — never at 0, never "nowhere". A job that fits no printer at all is simply never
routed: there is no home for it under the policy that decided its fits.

:func:`components` splits a farm into parts that cannot affect each other — a job and the
printers it fits, printers sharing a stagger group, jobs sharing an SJF group — so a change can
be replayed in its own part only; the result is the same as replaying everything.
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass, field
from math import inf
from typing import TYPE_CHECKING

from backend.app.services.farm_forecast import _admit

if TYPE_CHECKING:
    from backend.app.services.farm_forecast import StaggerPolicy
    from backend.app.services.forecast_operator_hours import OperatorHours


@dataclass(frozen=True)
class Held:
    """Work a printer already holds: its running print (``started``) or a row of its own queue."""

    seconds: float | None  # None = no estimate
    prep: float = 0.0
    started: bool = False
    auto_eject: bool = False
    #: Never starts here: a row of an order that may not use this printer keeps it busy for the
    #: router for good, and nothing behind it starts either.
    blocked: bool = False


@dataclass(frozen=True)
class Lane:
    """One physical printer — one lane, whatever models it is compatible with."""

    printer_id: int
    held: tuple[Held, ...] = ()
    #: May it receive auto-queue work at all (accepting, online)? A printer that may not still
    #: finishes what it holds, and its starts still take stagger windows.
    routable: bool = True
    #: Ready now — the router's first rank, for a printer that is free now.
    ready: bool = False
    #: What holds its next start now (the plate awaited, a blocking drying cycle).
    waiting: float = 0.0
    #: Held now with no forecast release: an HA interlock, a busy claim with no timed print.
    hold_unknown: bool = False
    #: Its queue starts nothing (paused or in error): the router still places a job there, where
    #: it waits for the operator — no finish (``"none"``, as no home at all: the agreed exclusion of
    #: such a printer) — and the printer never frees in the forecast. Nothing starts, so it takes
    #: no start window and makes no other job uncertain.
    stuck: bool = False
    gap: float = 0.0  # plate service after every print that is not auto-ejected
    requires_operator: bool = False
    #: Its own stagger interval; 0 = the farm's (``farm_forecast``: ``stagger_interval_seconds or`` the policy's).
    interval: float = 0.0


@dataclass(frozen=True)
class Job:
    """A pending auto-queue row as the router sees it."""

    job_id: int
    position: int
    #: The SJF grouping column — the row's raw ``target_model``.
    group: str | None
    seconds: float | None  # None = no estimate
    #: ``(printer_id, exact model, colour matches)`` of every printer that can print it now.
    fits: tuple[tuple[int, bool, int], ...] = ()
    been_jumped: bool = False
    auto_eject: bool = False
    prep: float = 0.0


class ReplayBudget(Exception):
    """The replay ran out of its work budget before the farm was dealt out."""


@dataclass
class Replay:
    finish: dict[int, float] = field(default_factory=dict)  # job → when its print ends
    unknown: set[int] = field(default_factory=set)  # job → its end cannot be known
    lane_of: dict[int, int] = field(default_factory=dict)  # job → the printer the router gives it
    routed_at: dict[int, float] = field(default_factory=dict)
    ready_when_routed: dict[int, bool] = field(default_factory=dict)
    work: int = 0
    #: Stopped before the farm was dealt out (``watch`` / ``until``): a watched job that is
    #: still ``"none"`` starts after ``until`` — never earlier.
    cut: bool = False

    def outcome(self, job_id: int) -> tuple[str, float | None]:
        """``("known", t)``, ``("unknown", None)``, or ``("none", None)`` — it never prints in the
        forecast: no printer fits it, or the one it is placed on starts nothing (``Lane.stuck``)."""
        if job_id in self.unknown:
            return "unknown", None
        if job_id in self.finish:
            return "known", self.finish[job_id]
        return "none", None


def router_key(job: Job, *, sjf: bool, jumped: bool | None = None) -> tuple:
    """The router's order (``auto_queue_scheduler._fetch_pending``), with the id as its last key.

    SJF: the model column first (NULL first, as SQLite sorts it), ``been_jumped`` DESC, the
    time ASC with no estimate last, then the position; otherwise the position. The id breaks
    ties that were undefined before both sides carried it.
    """
    if not sjf:
        return (job.position, job.job_id)
    marked = job.been_jumped if jumped is None else jumped
    group = (0, "") if job.group is None else (1, job.group)
    seconds = (0, job.seconds) if job.seconds is not None else (1, 0.0)
    return (group, 0 if marked else 1, seconds, job.position, job.job_id)


def jumps(started: Job, peer: Job, *, peer_jumped: bool) -> bool:
    """Whether placing ``started`` marks ``peer`` ``been_jumped`` (``_mark_jumped_peers``)."""
    if started.seconds is None or peer_jumped or started.group is None or peer.group != started.group:
        return False
    if peer.position >= started.position:
        return False
    return peer.seconds is None or peer.seconds > started.seconds


@dataclass(slots=True)
class _Run:
    seconds: float | None
    prep: float
    auto_eject: bool
    started: bool = False
    job: int | None = None
    blocked: bool = False


def replay(
    lanes: list[Lane] | tuple[Lane, ...],
    jobs: list[Job] | tuple[Job, ...],
    *,
    sjf: bool = False,
    stagger: StaggerPolicy | None = None,
    operator_hours: OperatorHours | None = None,
    budget: int | None = None,
    watch: frozenset[int] | set[int] | None = None,
    until: float | None = None,
) -> Replay:
    """Deal ``jobs`` out over ``lanes`` from now on, as the router would; see the module docstring.

    With ``watch``, stop once every watched job has a known start or is unknown — a print's end
    is fixed when it starts — or, with ``until`` too, once the clock passes it: a watched job not
    started by then ends later. A trial needs no more than that (spec T20–T21); each answer it
    gives is the whole replay's.
    """
    out = Replay()
    lanes = sorted(lanes, key=lambda lane: lane.printer_id)
    by_pid = {lane.printer_id: lane for lane in lanes}
    jobs = list(jobs)
    n = len(jobs)
    # Per job: its fit printers that are lanes here, as a dict printer → rank tail.
    # One dict per distinct set of fits: hundreds of copies of one job share it, and a replay
    # that allocates per copy and printer feeds the collector (T22: a full collection stalls the
    # event loop too).
    # A class is one distinct set of fits: its jobs in the router's order, with a cursor past
    # the ones already dealt (each job is dealt once, so the cursors move forward only). The
    # best job for the free printers is the best head among the classes that fit one of them —
    # what scanning every printer's own list gave, set up per class instead of per copy and
    # printer.
    class_of_fits: dict[tuple, int] = {}
    fit: list[dict[int, tuple[bool, int]]] = []  # per class
    cls: list[int] = []  # per job: its class
    for job in jobs:
        c = class_of_fits.get(job.fits)
        if c is None:
            c = class_of_fits[job.fits] = len(fit)
            fit.append(
                {pid: (exact, colour) for pid, exact, colour in job.fits if pid in by_pid and by_pid[pid].routable}
            )
        cls.append(c)
    class_jobs: list[list[int]] = [[] for _ in fit]
    for j in range(n):
        class_jobs[cls[j]].append(j)
    lane_classes: dict[int, list[int]] = {}
    for c, fitted in enumerate(fit):
        for pid in fitted:
            lane_classes.setdefault(pid, []).append(c)
    #: The earliest moment any printer of a class is tainted — ``uncertain`` in one lookup.
    class_taint: list[float] = [inf] * len(fit)
    jumped = [job.been_jumped for job in jobs]
    keys = [router_key(job, sjf=sjf) for job in jobs]
    done = [False] * n
    group_jobs: dict[str, list[int]] = {}
    if sjf:
        for j, job in enumerate(jobs):
            if job.group is not None:
                group_jobs.setdefault(job.group, []).append(j)
    cursor: list[int] = [0] * len(fit)
    #: Each job's place in the order the open wave was sorted in. A mark changes ``keys`` for
    #: the NEXT wave; the open one keeps its order, as the router's pass keeps the list it
    #: fetched (T10) — comparing the marked keys inside a wave would let a job jump it.
    rank: list[int] = [0] * n

    def rebuild() -> None:
        for place, j in enumerate(sorted(range(n), key=keys.__getitem__)):
            rank[j] = place
        for c, members in enumerate(class_jobs):
            members.sort(key=rank.__getitem__)
            cursor[c] = 0

    rebuild()
    dirty = False

    tainted: dict[int, float] = {}
    group_taint: dict[str, float] = {}
    peers: dict[int, frozenset[int]] = {}
    if stagger is not None:
        groups = {lane.printer_id: set(stagger.resolver.groups_for(lane.printer_id)) for lane in lanes}
        for lane in lanes:
            mine = groups[lane.printer_id]
            peers[lane.printer_id] = frozenset(
                pid for pid, theirs in groups.items() if pid != lane.printer_id and mine & theirs
            )
    windows: dict[int, float] = {pid: float(end) for pid, end in stagger.live.items()} if stagger else {}

    queue: dict[int, deque[_Run]] = {lane.printer_id: deque() for lane in lanes}
    ready_at: dict[int, float] = {}
    initially_free: set[int] = set()
    heap: list[tuple[float, int, int]] = []
    free: set[int] = set()

    def spend(units: int = 1) -> None:
        out.work += units
        if budget is not None and out.work > budget:
            raise ReplayBudget()

    def taint(pid: int, at: float) -> None:
        if at < tainted.get(pid, inf):
            tainted[pid] = at
            for c in lane_classes.get(pid, ()):
                if at < class_taint[c]:
                    class_taint[c] = at

    def taint_groups(pid: int, at: float) -> None:
        for other in peers.get(pid, ()):
            taint(other, at)

    def has_pending(c: int) -> bool:
        members = class_jobs[c]
        at = cursor[c]
        while at < len(members) and done[members[at]]:
            at += 1
        cursor[c] = at
        return at < len(members)

    def freeze(pid: int, at: float) -> None:
        """The lane's future is unknown from ``at``: whatever waits on it ends unknown.

        Its start group shares the uncertainty only when something can still start there — a
        queued run, or a pending job that may be routed to it once it frees: a printer nothing
        can reach takes no window, whenever its print ends (final review C1)."""
        taint(pid, at)
        if any(not run.started for run in queue[pid]) or any(has_pending(c) for c in lane_classes.get(pid, ())):
            taint_groups(pid, at)
        for run in queue[pid]:
            if run.job is not None:
                out.unknown.add(run.job)
        queue[pid].clear()

    def service(lane: Lane, finish: float, auto_eject: bool) -> float:
        gap = 0.0 if auto_eject else lane.gap
        if operator_hours is not None and lane.requires_operator and not auto_eject:
            return operator_hours.service_finish(finish, gap)
        return finish + gap

    for lane in lanes:
        pid = lane.printer_id
        held = [h for h in lane.held if h.seconds is None or h.seconds > 0]
        for h in held:
            queue[pid].append(
                _Run(h.seconds, 0.0 if h.started else h.prep, h.auto_eject, started=h.started, blocked=h.blocked)
            )
        if held:
            if held[0].started:
                heapq.heappush(heap, (0.0, 1, pid))
            elif lane.hold_unknown:
                freeze(pid, 0.0)
            else:
                heapq.heappush(heap, (lane.waiting, 1, pid))
        elif lane.routable:
            ready_at[pid] = inf if lane.hold_unknown else lane.waiting
            initially_free.add(pid)
            heapq.heappush(heap, (0.0, 0, pid))

    def is_ready(pid: int, t: float) -> bool:
        if t == 0.0 and pid in initially_free:
            # The pass's fact and the router's rank (spec A19, T-A25) — never a forecast.
            return by_pid[pid].ready
        return ready_at.get(pid, inf) <= t

    def uncertain(j: int, t: float) -> bool:
        if class_taint[cls[j]] <= t:
            return True
        return sjf and jobs[j].group is not None and group_taint.get(jobs[j].group, inf) <= t

    def assign(j: int, pid: int, t: float) -> None:
        nonlocal dirty
        spend()
        job = jobs[j]
        done[j] = True
        free.discard(pid)
        out.lane_of[job.job_id] = pid
        out.routed_at[job.job_id] = t
        out.ready_when_routed[job.job_id] = is_ready(pid, t)
        doubtful = uncertain(j, t)
        if doubtful:
            out.unknown.add(job.job_id)
            taint(pid, t)
            if sjf and job.group is not None:
                group_taint[job.group] = min(group_taint.get(job.group, inf), t)
        queue[pid].append(_Run(job.seconds, job.prep, job.auto_eject, job=job.job_id))
        if by_pid[pid].stuck:
            pass  # never prints there without the operator: no finish, and the printer never frees
        elif ready_at[pid] == inf:
            freeze(pid, t)
        else:
            heapq.heappush(heap, (max(t, ready_at[pid]), 1, pid))
        if sjf and job.seconds is not None and job.group is not None:
            for k in group_jobs[job.group]:
                if not done[k] and jumps(job, jobs[k], peer_jumped=jumped[k]):
                    jumped[k] = True
                    keys[k] = router_key(jobs[k], sjf=True, jumped=True)
                    dirty = True

    def wave(t: float) -> None:
        nonlocal dirty
        if dirty:
            rebuild()
            dirty = False
        while free:
            best = -1
            seen: set[int] = set()
            for pid in free:
                for c in lane_classes.get(pid, ()):
                    if c in seen:
                        continue
                    seen.add(c)
                    members = class_jobs[c]
                    at = cursor[c]
                    while at < len(members) and done[members[at]]:
                        at += 1
                        spend()
                    cursor[c] = at
                    if at < len(members) and (best < 0 or rank[members[at]] < rank[best]):
                        best = members[at]
            if best < 0:
                free.clear()  # nothing pending fits a free printer, now or later
                return
            fitted = fit[cls[best]]
            choice = max(
                (pid for pid in free if pid in fitted),
                key=lambda pid: (is_ready(pid, t), fitted[pid][0], fitted[pid][1], -pid),
            )
            assign(best, choice, t)

    def start(pid: int, t: float) -> None:
        lane = by_pid[pid]
        if not queue[pid]:
            return
        run = queue[pid][0]
        if not run.started and (lane.stuck or run.blocked):
            return  # starts nothing, for good: the printer stays held, with no window
        if not run.started and lane.hold_unknown:
            freeze(pid, t)  # held with no forecast release: when it starts nobody knows
            return
        if not run.started and stagger is not None:
            admit = _admit(t, lane, windows, stagger)
            if admit > t:
                heapq.heappush(heap, (admit, 1, pid))
                return
            windows[pid] = t + (run.prep if stagger.wait_for_bed else 0.0) + (lane.interval or stagger.interval_seconds)
        queue[pid].popleft()
        doubtful = tainted.get(pid, inf) <= t
        if doubtful and not run.started:
            taint_groups(pid, t)
        if run.seconds is None:
            if run.job is not None:
                out.unknown.add(run.job)
            freeze(pid, t)
            return
        finish = t + run.prep + run.seconds
        if run.job is not None:
            if doubtful:
                out.unknown.add(run.job)
            elif run.job not in out.unknown:
                out.finish[run.job] = finish
        ready = service(lane, finish, run.auto_eject)
        if run.started:
            ready = max(ready, lane.waiting)
        if queue[pid]:
            heapq.heappush(heap, (ready, 1, pid))
        elif lane.routable:
            ready_at[pid] = inf if lane.hold_unknown else ready
            heapq.heappush(heap, (finish, 0, pid))

    watching = None if watch is None else frozenset(watch)
    while heap:
        t = heap[0][0]
        if watching is not None and (
            (until is not None and t > until) or all(j in out.finish or j in out.unknown for j in watching)
        ):
            out.cut = True
            break
        while heap and heap[0][0] == t and heap[0][1] == 0:
            _, _, pid = heapq.heappop(heap)
            spend()
            free.add(pid)
        if free:
            wave(t)
        while heap and heap[0][0] == t and heap[0][1] == 1:
            _, _, pid = heapq.heappop(heap)
            spend()
            start(pid, t)

    for j, job in enumerate(jobs):
        if not done[j] and class_taint[cls[j]] < inf:
            out.unknown.add(job.job_id)
    for job_id in out.unknown:
        out.finish.pop(job_id, None)
    return out


def components(
    lanes: list[Lane] | tuple[Lane, ...],
    jobs: list[Job] | tuple[Job, ...],
    *,
    sjf: bool = False,
    stagger: StaggerPolicy | None = None,
) -> list[tuple[frozenset[int], frozenset[int]]]:
    """The farm split into ``(printer ids, job ids)`` parts that cannot affect each other.

    Joined: a job and every printer it fits; printers sharing a stagger group; with SJF, jobs
    sharing a model group (a placement re-orders its group). A part replayed alone ends exactly
    as it does in the whole replay.

    Asked once per committed move over hundreds of copies of one job, so it joins each distinct
    set of fit printers once and hangs every job on it, over integer nodes.
    """
    lane_ids = sorted({lane.printer_id for lane in lanes})
    index = {pid: i for i, pid in enumerate(lane_ids)}
    parent = list(range(len(lane_ids)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    if stagger is not None:
        first: dict[object, int] = {}
        for pid in lane_ids:
            for group in stagger.resolver.groups_for(pid):
                if group in first:
                    union(first[group], index[pid])
                else:
                    first[group] = index[pid]
    anchors: dict[tuple, int | None] = {}
    groups: dict[str, int] = {}
    nodes: list[int] = []
    for job in jobs:
        anchor = anchors.get(job.fits, -1)
        if anchor == -1:
            fitted = [index[pid] for pid, _exact, _colour in job.fits if pid in index]
            anchor = fitted[0] if fitted else None
            for other in fitted[1:]:
                union(fitted[0], other)
            anchors[job.fits] = anchor
        if anchor is None:
            # A job no printer here fits: a part of its own, unless its SJF group joins it.
            parent.append(len(parent))
            anchor = len(parent) - 1
        if sjf and job.group is not None:
            if job.group in groups:
                union(groups[job.group], anchor)
            else:
                groups[job.group] = anchor
        nodes.append(anchor)
    parts: dict[int, tuple[set[int], set[int]]] = {}
    for pid in lane_ids:
        parts.setdefault(find(index[pid]), (set(), set()))[0].add(pid)
    for job, node in zip(jobs, nodes, strict=True):
        parts.setdefault(find(node), (set(), set()))[1].add(job.job_id)
    return [(frozenset(printers), frozenset(members)) for printers, members in parts.values()]
