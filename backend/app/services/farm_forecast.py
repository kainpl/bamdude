"""Farm forecast — when an order is ready, and how a line splits across models.

Spec: docs/superpowers/specs/2026-09-06-farm-forecast-design.md. An ADVISORY
layer above the plan engine and the queues: it reads the plan's rows and the
database's picture of the farm, runs the textbook list-scheduling makespan, and
answers with dates and counts. It gates nothing and writes nothing, and it never
reads a printer beyond its model and what its queue already holds — routing is
not dispatching, and the plan engine stays ignorant of printers.

Two ETAs travel together (Decision 2): ``now`` — this order's plan sent on top of
what the queues hold today; ``after`` — after the plans of every active order
ranked ahead of it. Unknown estimates are counted, never defaulted (Decision 6).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.project import Project
from backend.app.services.filament_intake import loaded_descriptor
from backend.app.services.order_filing import priority_rank
from backend.app.services.plan_engine import OrderPlan, plan_for_orders
from backend.app.services.queue_times import print_time_for_row
from backend.app.utils.printer_models import normalize_model_name

#: What the simulation does NOT model in this version; every surface shows it.
ASSUMPTIONS: tuple[str, ...] = ("stagger", "plate_clear", "drying", "prep")


def model_key(model: str | None) -> str | None:
    """The auto-queue's comparison key — both sides through the same normaliser."""
    normalised = normalize_model_name(model) if model else None
    if not normalised:
        return None
    return normalised.strip().lower() or None


# ---------- inputs ----------


@dataclass
class QueuedRow:
    order_id: int | None
    seconds: int | None


@dataclass
class MachineState:
    printer_id: int
    model: str | None
    running_seconds: float = 0.0
    queued: list[QueuedRow] = field(default_factory=list)  # position order
    #: May this machine RECEIVE new work? Availability never decides what a
    #: machine OWES (Decision 7): a parked printer — inactive, operator-paused
    #: or its queue in ``paused``/``error`` — still finishes what it holds, so
    #: its rows still date their orders and still count in the farm's «free
    #: at»; only the placement step skips it.
    accepts_new_work: bool = True


@dataclass
class StagedJob:
    order_id: int | None
    target_model: str | None
    seconds: int | None


@dataclass
class FarmSnapshot:
    printers: list[MachineState]
    staged: list[StagedJob]


@dataclass
class PlateOption:
    plate_id: int
    model: str | None
    seconds: int | None


@dataclass
class PrintJob:
    order_id: int
    line_id: int
    row_plate_id: int
    options: list[PlateOption]  # the row's own plate first, then its alternatives


# ---------- outputs ----------


@dataclass
class RowForecast:
    plate_id: int
    proposed_split: (
        dict[int, int] | None
    )  # ProductPlate.id → prints, summing to the row's count; None for a row without alternatives or one the farm could not place


@dataclass
class LineForecast:
    line_id: int
    now_eta: datetime | None
    now_seconds: int | None
    after_eta: datetime | None
    after_seconds: int | None
    unknown_prints: int
    unroutable_prints: int
    rows: list[RowForecast] = field(default_factory=list)


@dataclass
class OrderForecast:
    project_id: int
    now_eta: datetime | None
    now_seconds: int | None
    after_eta: datetime | None
    after_seconds: int | None
    machine_seconds: int | None
    unknown_prints: int
    unroutable_prints: int
    ahead_count: int
    lines: list[LineForecast] = field(default_factory=list)


@dataclass
class PrinterForecast:
    """One machine's share of the farm's «free at»: when IT is free, given
    what it already holds — the running head, its pending rows, and whatever
    staged auto-queue work the simulation dealt to it. The two «free at»
    sorts (printers page, queue page) read this instead of re-deriving it in
    the browser, so the card order and the queue tile can never disagree."""

    printer_id: int
    free_seconds: int
    #: This machine's own rows without an estimate — why ITS number can read
    #: zero while the machine is busy (the farm's counter, per printer).
    unknown_prints: int = 0


@dataclass
class FarmForecast:
    free_seconds: int
    #: Σ rows of the snapshot with no estimate — queued rows (the running head
    #: among them) and staged auto-queue jobs, whatever order they belong to.
    #: The queue tile says with it why «free at» reads what it reads.
    unknown_prints: int = 0
    #: Every machine of the snapshot, parked ones included, in snapshot order.
    printers: list[PrinterForecast] = field(default_factory=list)


# ---------- the simulation ----------


@dataclass
class _Machine:
    printer_id: int
    key: str | None
    free_at: float
    accepts_new_work: bool = True


@dataclass
class _Placement:
    finish: float
    row_plate_id: int
    plate_id: int


@dataclass
class _State:
    machines: list[_Machine]
    order_finish: dict[int, float] = field(default_factory=dict)  # order → finish of its already queued/staged work
    unknown: Counter = field(default_factory=Counter)  # order_id → prints without an estimate
    unroutable: Counter = field(default_factory=Counter)  # order_id → prints with no printer for their model
    line_unknown: Counter = field(default_factory=Counter)  # the same two, per line
    line_unroutable: Counter = field(default_factory=Counter)
    #: The snapshot's own estimate-less rows, counted whether or not they name
    #: an order — the farm header's counter, not any order's.
    farm_unknown: int = 0
    #: The same rows, per machine — a queued row belongs to exactly one
    #: printer, so these sum to ``farm_unknown`` minus the staged jobs.
    machine_unknown: Counter = field(default_factory=Counter)

    def farm_finish(self) -> float:
        return max((m.free_at for m in self.machines), default=0.0)


def _bump(finish: dict[int, float], order_id: int | None, at: float) -> None:
    if order_id is not None:
        finish[order_id] = max(finish.get(order_id, 0.0), at)


def _earliest(machines: list[_Machine], key: str | None) -> _Machine | None:
    """The soonest-free machine of ``key`` that may take new work — a parked
    printer owes what it holds but is never given more (Decision 7)."""
    if key is None:
        return None
    candidates = [m for m in machines if m.key == key and m.accepts_new_work]
    return min(candidates, key=lambda m: (m.free_at, m.printer_id)) if candidates else None


def _initial_state(snapshot: FarmSnapshot) -> _State:
    """What every printer already owes, then the staging area dealt out longest-first.

    Every machine of the snapshot is walked, parked ones included: what a
    printer already holds finishes there whether or not it may take more.
    """
    state = _State(machines=[])
    for machine in snapshot.printers:
        t = max(0.0, float(machine.running_seconds))
        for row in machine.queued:
            if row.seconds is None:
                state.farm_unknown += 1
                state.machine_unknown[machine.printer_id] += 1
                if row.order_id is not None:
                    state.unknown[row.order_id] += 1
                continue
            if row.seconds <= 0:
                continue
            t += row.seconds
            _bump(state.order_finish, row.order_id, t)
        state.machines.append(
            _Machine(
                printer_id=machine.printer_id,
                key=model_key(machine.model),
                free_at=t,
                accepts_new_work=machine.accepts_new_work,
            )
        )
    for job in sorted(snapshot.staged, key=lambda j: -(j.seconds or 0)):
        if not job.seconds or job.seconds <= 0:
            state.farm_unknown += 1
            if job.order_id is not None:
                state.unknown[job.order_id] += 1
            continue
        target = _earliest(state.machines, model_key(job.target_model))
        if target is None:
            if job.order_id is not None:
                state.unroutable[job.order_id] += 1
            continue
        target.free_at += job.seconds
        _bump(state.order_finish, job.order_id, target.free_at)
    return state


def _place(state: _State, jobs: list[PrintJob]) -> dict[int, list[_Placement]]:
    """Deal the jobs out longest-first (a job's length is its shortest option);
    each goes to the option whose earliest-free printer finishes it first.
    Returns ``line_id → placements``; the unknown / unroutable ones are counted
    on their order and their line."""
    placed: dict[int, list[_Placement]] = {}

    def length(job: PrintJob) -> int:
        return min((o.seconds for o in job.options if o.seconds and o.seconds > 0), default=0)

    for job in sorted(jobs, key=lambda j: -length(j)):
        timed = [o for o in job.options if o.seconds and o.seconds > 0]
        if not timed:
            state.unknown[job.order_id] += 1
            state.line_unknown[job.line_id] += 1
            continue
        best: tuple[float, int, _Machine, PlateOption] | None = None
        for option in timed:
            machine = _earliest(state.machines, model_key(option.model))
            if machine is None:
                continue
            candidate = (machine.free_at + option.seconds, option.seconds, machine, option)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        if best is None:
            state.unroutable[job.order_id] += 1
            state.line_unroutable[job.line_id] += 1
            continue
        finish, _secs, machine, option = best
        machine.free_at = finish
        placed.setdefault(job.line_id, []).append(_Placement(finish, job.row_plate_id, option.plate_id))
    return placed


def jobs_from_plan(order_id: int, plan: OrderPlan | None) -> list[PrintJob]:
    """One job per print the plan asks for; a row's options are its own plate
    then its alternatives — the same list the plan block's split offers."""
    jobs: list[PrintJob] = []
    for line in plan.lines if plan else []:
        for row in line.rows:
            options = [PlateOption(row.plate_id, row.printer_model, row.print_time_seconds)] + [
                PlateOption(a.plate_id, a.printer_model, a.print_time_seconds) for a in row.alternatives
            ]
            jobs.extend(PrintJob(order_id, line.line_id, row.plate_id, options) for _ in range(row.count))
    return jobs


def machine_seconds_of(plan: OrderPlan | None) -> int | None:
    """Σ count × estimate over rows with one; None when rows exist but none has
    an estimate; 0 for an order with nothing left to plan."""
    rows = [r for line in (plan.lines if plan else []) for r in line.rows]
    if not rows:
        return 0
    timed = [r for r in rows if r.print_time_seconds and r.print_time_seconds > 0]
    return sum(r.count * r.print_time_seconds for r in timed) if timed else None


def simulate_farm(snapshot: FarmSnapshot) -> FarmForecast:
    """When the last printer is free, given what the queues already hold — and
    how many of those rows carry no estimate, so a small number can say why.
    Beside the farm's number, every machine's own: the same walk, read per
    printer, so a «free at» order of the cards is the queue tile's arithmetic."""
    state = _initial_state(snapshot)
    return FarmForecast(
        free_seconds=int(round(state.farm_finish())),
        unknown_prints=state.farm_unknown,
        printers=[
            PrinterForecast(
                printer_id=m.printer_id,
                free_seconds=int(round(m.free_at)),
                unknown_prints=state.machine_unknown[m.printer_id],
            )
            for m in state.machines
        ],
    )


def _eta(now: datetime, seconds: float | None) -> tuple[datetime | None, int | None]:
    if seconds is None:
        return None, None
    whole = int(round(seconds))
    return now + timedelta(seconds=whole), whole


def _order_result(
    order_id: int,
    plan: OrderPlan | None,
    state: _State,
    placed: dict[int, list[_Placement]],
    now: datetime,
    ahead: int,
) -> OrderForecast:
    """Read one run's outcome for ``order_id`` off the state it left: the order
    finishes when its last planned print AND its already-queued work finish."""
    finishes: list[float] = []
    if order_id in state.order_finish:
        finishes.append(state.order_finish[order_id])
    lines: list[LineForecast] = []
    for line in plan.lines if plan else []:
        line_placements = placed.get(line.line_id, [])
        line_finish = max((p.finish for p in line_placements), default=None)
        if line_finish is not None:
            finishes.append(line_finish)
        eta, secs = _eta(now, line_finish)
        rows: list[RowForecast] = []
        for row in line.rows:
            if not row.alternatives:
                rows.append(RowForecast(plate_id=row.plate_id, proposed_split=None))
                continue
            split = {row.plate_id: 0, **{a.plate_id: 0 for a in row.alternatives}}
            for p in line_placements:
                if p.row_plate_id == row.plate_id:
                    split[p.plate_id] = split.get(p.plate_id, 0) + 1
            proposed_split = split if sum(split.values()) == row.count else None
            rows.append(RowForecast(plate_id=row.plate_id, proposed_split=proposed_split))
        lines.append(
            LineForecast(
                line_id=line.line_id,
                now_eta=eta,
                now_seconds=secs,
                after_eta=None,
                after_seconds=None,
                unknown_prints=state.line_unknown[line.line_id],
                unroutable_prints=state.line_unroutable[line.line_id],
                rows=rows,
            )
        )
    eta, secs = _eta(now, max(finishes) if finishes else None)
    return OrderForecast(
        project_id=order_id,
        now_eta=eta,
        now_seconds=secs,
        after_eta=None,
        after_seconds=None,
        machine_seconds=machine_seconds_of(plan),
        unknown_prints=state.unknown[order_id],
        unroutable_prints=state.unroutable[order_id],
        ahead_count=ahead,
        lines=lines,
    )


def forecast_orders(
    snapshot: FarmSnapshot,
    plans: dict[int, OrderPlan],
    ordered_ids: list[int],
    targets: set[int],
    now: datetime,
) -> dict[int, OrderForecast]:
    """``now`` and ``after`` for every target, walking ``ordered_ids`` in rank order.

    ``after`` is read off ONE running state that every order's plan advances in
    turn; ``now`` is read off a fresh initial state per target. The proposed
    split comes from the ``now`` run — it is what the button enqueues.
    """
    ahead_state = _initial_state(snapshot)
    out: dict[int, OrderForecast] = {}
    for index, order_id in enumerate(ordered_ids):
        plan = plans.get(order_id)
        jobs = jobs_from_plan(order_id, plan)
        if order_id not in targets:
            _place(ahead_state, jobs)
            continue
        fresh = _initial_state(snapshot)
        result = _order_result(order_id, plan, fresh, _place(fresh, jobs), now, ahead=index)
        after = _order_result(order_id, plan, ahead_state, _place(ahead_state, jobs), now, ahead=index)
        result.after_eta, result.after_seconds = after.now_eta, after.now_seconds
        for line, after_line in zip(result.lines, after.lines, strict=True):
            line.after_eta, line.after_seconds = after_line.now_eta, after_line.now_seconds
        out[order_id] = result
    return out


# ---------- the loader ----------


async def load_snapshot(db: AsyncSession, now: datetime) -> FarmSnapshot:
    """What the farm already owes, from the database alone.

    Machines = every printer that is not archived. Availability decides who may
    RECEIVE work, never what a machine OWES (Decision 7): an inactive or paused
    printer stays in the snapshot with ``accepts_new_work=False``, so its
    running and queued rows still date their orders and still count in the
    farm's «free at» — dropping it made that work vanish with no counter.
    Running = the HEAD of the machine's queued work: the printing archive,
    timed by its remaining seconds (estimate minus elapsed, never negative; no
    ``started_at`` = the whole estimate), or ``None`` when it has no estimate
    yet. Queued = the queue's pending rows in position order, timed by the
    reader the queue response uses. Staged = pending auto-queue rows nobody has
    handed to a printer yet. ``PrinterQueue.id == printer_id`` is the invariant
    the queued-rows join leans on.
    """
    # Columns, not the ``Printer`` entity: hydrating the mapped object would
    # fire its ``lazy="selectin"`` relationships (``location``, ``tags``) as a
    # second, unwanted "FROM printers" round trip on every call.
    printers = (
        await db.execute(
            select(Printer.id, Printer.model, Printer.is_active, PrinterQueue.status, PrinterQueue.is_paused)
            .outerjoin(PrinterQueue, PrinterQueue.printer_id == Printer.id)
            .where(Printer.archived.is_(False))
        )
    ).all()
    machines: dict[int, MachineState] = {}
    for printer_id, model, is_active, queue_status, is_paused in printers:
        machines[printer_id] = MachineState(
            printer_id=printer_id,
            model=model,
            accepts_new_work=bool(is_active) and not is_paused and queue_status not in ("paused", "error"),
        )
    staged = await _staged(db)
    if not machines:
        return FarmSnapshot(printers=[], staged=staged)
    running = (
        await db.execute(
            select(
                PrintArchive.printer_id,
                PrintArchive.print_time_seconds,
                PrintArchive.started_at,
                PrintArchive.project_id,
            ).where(
                PrintArchive.status == "printing",
                PrintArchive.deleted_at.is_(None),
                PrintArchive.printer_id.in_(list(machines)),
            )
        )
    ).all()
    for printer_id, estimate, started_at, project_id in running:
        machine = machines.get(printer_id)
        if machine is None:
            continue
        remaining = None
        if estimate:
            elapsed = (now - started_at).total_seconds() if started_at else 0.0
            remaining = int(round(max(0.0, estimate - elapsed)))
        # Appended before the pending-rows loop below, while ``queued`` is
        # still empty — this IS the head of the machine's queued work.
        machine.queued.append(QueuedRow(order_id=project_id, seconds=remaining))
    rows = (
        (
            await db.execute(
                select(PrintQueueItem)
                .options(
                    selectinload(PrintQueueItem.archive),
                    selectinload(PrintQueueItem.library_file),
                    # m173: a pending job is timed from the bytes it OWNS, which
                    # outlive both rows above — the forecast and the queue card must
                    # agree, and they share ``print_time_for_row`` to do it.
                    selectinload(PrintQueueItem.queue_source),
                )
                .where(PrintQueueItem.status == "pending", PrintQueueItem.queue_id.in_(list(machines)))
                .order_by(PrintQueueItem.queue_id, PrintQueueItem.position, PrintQueueItem.id)
            )
        )
        .scalars()
        .all()
    )
    for item in rows:
        machine = machines.get(item.queue_id)
        if machine is None:
            continue
        seconds = print_time_for_row(
            archive=item.archive,
            library_file=item.library_file,
            plate_id=item.plate_id,
            descriptor=loaded_descriptor(item),
        )
        machine.queued.append(QueuedRow(order_id=item.project_id, seconds=seconds))
    return FarmSnapshot(printers=list(machines.values()), staged=staged)


async def _staged(db: AsyncSession) -> list[StagedJob]:
    rows = (
        await db.execute(
            select(AutoQueueItem.project_id, AutoQueueItem.target_model, AutoQueueItem.print_time_seconds).where(
                AutoQueueItem.status == "pending", AutoQueueItem.assigned_to_item_id.is_(None)
            )
        )
    ).all()
    return [StagedJob(order_id=pid, target_model=model, seconds=secs) for pid, model, secs in rows]


async def rank_active_orders(db: AsyncSession) -> list[int]:
    """Every active order, most urgent first — the candidates' rule: priority,
    then due date (none last), then age, then id."""
    rows = (
        await db.execute(
            select(Project.id, Project.priority, Project.due_date, Project.created_at).where(Project.status == "active")
        )
    ).all()
    ranked = sorted(
        rows,
        key=lambda r: (-priority_rank(r[1]), r[2] is None, r[2] or datetime.min, r[3] or datetime.min, r[0]),
    )
    return [pid for pid, _priority, _due, _created in ranked]


def _empty_forecast(project_id: int) -> OrderForecast:
    """A closed order's answer: it exists, and nothing about it is planned."""
    return OrderForecast(
        project_id=project_id,
        now_eta=None,
        now_seconds=None,
        after_eta=None,
        after_seconds=None,
        machine_seconds=0,
        unknown_prints=0,
        unroutable_prints=0,
        ahead_count=0,
        lines=[],
    )


async def forecast_projects(
    db: AsyncSession, project_ids: list[int], now: datetime
) -> tuple[FarmForecast, dict[int, OrderForecast]]:
    """The batch: one snapshot, the ranking, the plans of the targets and of
    everything ranked ahead of any target, one simulation walk.

    A CLOSED order is never planned (Decision 9) — the product rule everywhere
    else is «closed = nothing is planned». An inactive id that exists answers
    the empty forecast, so neither route refuses it; an id that names no order
    at all is absent from the answer.
    """
    snapshot = await load_snapshot(db, now)
    ranked = await rank_active_orders(db)
    wanted = list(dict.fromkeys(project_ids))
    # Existence AND status in one statement: the walk needs to know which
    # wanted ids are active, and the answer needs to know which of the rest
    # exist at all.
    status_rows = (
        (await db.execute(select(Project.id, Project.status).where(Project.id.in_(wanted)))).all() if wanted else []
    )
    status_of: dict[int, str | None] = dict(status_rows)
    active_targets = {pid for pid in wanted if status_of.get(pid) == "active"}
    walk = ranked[: max((i for i, pid in enumerate(ranked) if pid in active_targets), default=-1) + 1]
    plans: dict[int, OrderPlan] = await plan_for_orders(db, walk) if walk else {}
    out = forecast_orders(snapshot, plans, walk, {pid for pid in active_targets if pid in plans}, now)
    for pid in wanted:
        if pid not in out and pid in status_of:
            out[pid] = _empty_forecast(pid)
    return simulate_farm(snapshot), out
