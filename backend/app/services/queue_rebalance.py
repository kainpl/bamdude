"""Move an order line's still-pending auto-queue work to idle printers of another model.

Spec: docs/superpowers/specs/2026-09-10-model-rebalancing-design.md.

The unit is PARTS of a line, not prints: a pending item is a claim on covering
``y_m`` parts, and moving it may replace one print of yield 6 by three prints
of yield 2 on a smaller bed, or by one print of a bigger plate with a surplus
of less than one plate's worth. :func:`plan_moves` is the pure decision —
dataclasses in, moves out — and :func:`rebalance` is the only thing here that
touches the database: it loads what the decision needs, calls it, and applies
each move through the writer every enqueue uses.

⚠️ **Routing is not dispatching, and this is routing.** Nothing here starts a
print, gates the ROUTER on readiness, or touches ``find_eligible_printer`` /
``_assign``. "Ready" is asked only to decide who may RECEIVE moved work, and
whether a home printer would take the job now: a printer with an empty queue
that still waits for a plate clear, or that a Home Assistant interlock holds,
only looks free, and moving work to it would move it nowhere — so readiness is
the busy set AND ``PrintScheduler._is_printer_idle`` AND the tick's interlock
set, the readiness signals the router's ranking already uses.

⚠️ **A printer is asked about THIS job, never about its model** (spec
rebalance-job-feasibility r2). A free P2S holding PLA and ABS is no home for a
PETG plate: it neither vetoes the move (``home_model_idle``) nor stands in for
the home's finish time, and a P1S without PETG is no receiver. Each answer is
the shared resolver's (``resolve_filament_routing``) over the job's own
requirements and policy and the printer's feed snapshot — see
:class:`_FitOracle`; a busy home is asked too, about the filament it holds now,
because the router's own facts never cover a busy printer (it checks busy
before it resolves). Capacity lives in one per-pass ledger, printer id →
seconds until free (:func:`plan_moves`).

The cheap verdicts are reached before the expensive load: a cooling line, and a
row with a free home printer that can print it (or might, unread), are both
answered without the plan engine's catalog. What is left genuinely needs it —
"a free printer with no candidate plate for THESE lines" cannot be answered
from the queue rows alone, and there is no cheaper source for it than the
plates themselves.

⚠️ **Never moved:** assigned, scheduled for a time, staged (``manual_start``),
pinned (slot-bound ``filament_overrides``), aimed at a location, sourced from
an archive, or not filed under an order line — see :func:`refusal`. Those
either belong to a printer already, carry an operator's explicit decision, or
have nothing to move to.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import hashlib
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field, replace as replace_job
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, Protocol

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core import database as _database
from backend.app.core.database import take_write_lock
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.project_line import ProjectLine
from backend.app.models.queue_source import QueueSource
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.schemas.auto_queue import AutoQueueItemCreate
from backend.app.schemas.project import RebalanceOut, RebalanceSkipped
from backend.app.services import queue_sources
from backend.app.services.auto_queue_add import add_items_to_auto_queue
from backend.app.services.auto_queue_eligibility import busy_printer_ids, router_view_conditions
from backend.app.services.farm_forecast import (
    FarmSnapshot,
    StaggerPolicy,
    load_snapshot,
    model_key,
    rank_active_orders,
)
from backend.app.services.filament_intake import (
    enrich_family_filament_types,
    read_item_requirements,
    resolve_source_path,
)
from backend.app.services.filament_policy import auto_policy
from backend.app.services.filament_preflight import feed_signature
from backend.app.services.filament_requirements import PrintRequirements, PrintRequirementsCache
from backend.app.services.filament_routing import RoutingPolicy, resolve_filament_routing
from backend.app.services.order_metrics import attribute, batch_contexts, line_accepts_materials
from backend.app.services.order_print_options import override_options
from backend.app.services.order_queue import awaiting_auto_row_conditions
from backend.app.services.plan_engine import (
    _pick_key,
    counted_parts_by_line,
    line_yield,
    plate_recipe_index,
    recipes_for_row,
)
from backend.app.services.print_option_defaults import preference_options
from backend.app.services.print_scheduler import scheduler
from backend.app.services.printer_location_service import load_tree, subtree_ids
from backend.app.services.printer_manager import printer_manager
from backend.app.services.product_composition import PlateRecipe, estimate_seconds, recipes_for_products
from backend.app.services.queue_source_capture import (
    StagedSource,
    capture_staged,
    discard_staged,
    plan_capture,
    publish_staged,
    staged_requirements,
)
from backend.app.services.rebalance_replay import (
    Held,
    Job,
    Lane,
    Replay,
    ReplayBudget,
    components,
    replay,
)
from backend.app.utils.model_compatibility import model_compatibility

if TYPE_CHECKING:
    from backend.app.services.forecast_operator_hours import OperatorHours
    from backend.app.services.printer_feed_snapshot import PrinterFeedSnapshot

logger = logging.getLogger(__name__)

REBALANCE_COOLDOWN_SECONDS = 300
REBALANCE_SETTING_KEY = "auto_queue_rebalance_models"
#: The router's SJF switch (``auto_queue_scheduler.SJF_SETTING_KEY``; not imported — that module imports this one).
SJF_SETTING_KEY = "queue_shortest_first"
#: Replay work units for one pass, every replay of the baseline and the trials together (spec T20).
#: Measured 2026-10-28 (stage 4): the reporter's 47-printer farm with 394 jobs, 358 × 16 and
#: 1000 × 32 spend 28 000–73 000 units; a 1000 × 32 farm where most trials are turned down over
#: one giant part spends 451 000 (≈ 2 s of decision). Beyond this the pass answers
#: ``forecast_budget`` and the moves already made stand.
REBALANCE_WORK_BUDGET = 600_000

#: The closed list of reasons an item is left where it is. The frontend
#: translates them under ``autoQueue.rebalance.skipped.<code>`` — adding one
#: here means adding it there, in both locales.
SKIP_REASONS = (
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
    # A home printer that would take the job now, but whose fit is unread (its feed not
    # reported yet, a nozzle unknown): the router may place the job there seconds from
    # now, so it is not moved away on a guess. Also: no fit receiver, but some unread.
    "eligibility_unknown",
    # No alternative plate of the line has a free printer that can print it.
    "no_eligible_target",
    # What the move was decided on changed while the target file was copied.
    "eligibility_changed",
    # The forecast cannot date the job at home, or every transferred print: a print without
    # an estimate, a printer held with no forecast release (spec rebalance-tail-time T18–T19).
    "forecast_incomplete",
    # The pass's forecast budget ran out before this job was decided (T20).
    "forecast_budget",
    # The owner's rule (T23): one automatic move per row, then by hand only.
    "already_rebalanced_auto",
    # An earlier move of this pass did not apply; the rest was decided in a world where it did (T17).
    "plan_invalidated_after_apply_failure",
)

#: How a capture refusal (spec §6's taxonomy) is reported on the panel.
#:
#: **Three buckets, not eight, and the split is by what the OPERATOR does** — the
#: panel's reason column is an instruction, not a diagnosis. Copying the target
#: file can fail seven ways and they fold onto three answers:
#:
#: * ``source_copy_busy`` — wait, and not even that: every cause is transient and
#:   self-clearing (all capture workers taken, the hydration slot taken, a restore
#:   having swapped the spool, a read that ran out of time), and a refused move
#:   sets no ``rebalanced_at``, so ``cooling_lines`` does not hold the line and the
#:   retry is the NEXT tick rather than 300 s later. The correct response is
#:   nothing.
#: * ``source_spool_full`` — free space on the server's data volume. The most
#:   actionable cause in the set, and folded into "unreadable" it sent the operator
#:   to look at the library file instead.
#: * ``source_unreadable`` — the file itself: gone, trashed, on an unreachable
#:   share, changed under the read, or not the container it claims to be. Nothing
#:   will ever succeed until a person fixes something.
#:
#: A plate the target file does not have stays in the last bucket deliberately: it
#: means the plan's catalog and the file disagree, which is a bug report rather
#: than an operator action. An unrecognised refusal lands there too — erring
#: towards "a person must look" is the safe direction for a code nobody mapped.
_SKIP_BY_CAPTURE_REASON = {
    "source_copy_busy": "source_copy_busy",
    "source_spool_replaced": "source_copy_busy",
    "source_copy_timeout": "source_copy_busy",
    "source_spool_no_space": "source_spool_full",
    "source_spool_write_failed": "source_spool_full",
}


def capture_skip_reason(exc: HTTPException) -> str:
    """Which of :data:`SKIP_REASONS` a capture refusal is reported as.

    Reads the machine code out of the ``routing_detail`` body the capture
    service's own mapper built (``{"code", "params", "message"}``) — never the
    HTTP status, which two different buckets share, and never the message, which
    is localized.
    """
    detail = exc.detail
    code = detail.get("code") if isinstance(detail, dict) else None
    return _SKIP_BY_CAPTURE_REASON.get(code, "source_unreadable")


# ---------- what the decision sees ----------


@dataclass(frozen=True)
class PlateOption:
    """One sliced candidate plate of a line, as the decision sees it."""

    plate_id: int  # ProductPlate.id
    library_file_id: int
    plate_index: int  # the recipe's index; 0 = the whole file
    model: str  # ``farm_forecast.model_key`` spelling — what idle capacity is keyed by
    model_label: str  # the file's own spelling (``P1S``) — what target_model and the profile lookup want
    yield_parts: int  # the plate's ``line_yield`` summed over the line's counted parts
    seconds: int | None  # ``estimate_seconds(recipe)``; None = no estimate, never a target


@dataclass(frozen=True)
class MovableItem:
    """A pending row — a candidate to move, or (in ``FarmView.tail``) a job the router deals out
    ahead of or beside the candidates, which therefore takes printer time."""

    item_id: int
    line_id: int | None
    home_model: str  # ``model_key`` of the item's target_model
    yield_parts: int  # y_m — what this item covers for its line
    seconds: int | None  # t_m; None = no estimate, never 0
    position: int = 0
    #: The router's SJF grouping column — the row's raw ``target_model``.
    group: str | None = None
    been_jumped: bool = False
    auto_eject: bool = False
    prep: float = 0.0  # what it pays before its start


Verdict = Literal["yes", "no", "unknown"]

#: The reason a TARGET fit is unknown because the alternative plate's own file
#: could not be read: reported as the existing ``source_unreadable`` when nothing
#: else stood in the way (spec R17 — source errors keep their reasons).
TARGET_SOURCE_UNREADABLE = "target_source_unreadable"


@dataclass(frozen=True)
class Fit:
    """Can one printer print one job — the shared resolver's answer, reduced to what the decision reads.

    ``yes`` carries the plan's colour matches, the router's third ranking key. A
    plan itself never leaves the oracle: the router resolves its own at placement.
    """

    verdict: Verdict
    reason: str | None = None
    color_matches: int = 0


@dataclass(frozen=True)
class Machine:
    """One printer of the pass — a lane of the replay (:mod:`rebalance_replay`).

    ``ready`` is a fact of the pass, never derived from a forecast: a printer
    awaiting its plate clear, held by a Home Assistant interlock or in the busy
    set may well be forecast at 0 s (spec A19), and still receives nothing. It is
    also the router's rank (``find_eligible_printer``: idle, plate clear, not
    interlocked). ``dispatchable`` is whether ``check_queue`` would start anything
    from its queue: the router places on a queue in error all the same, where
    the work waits for the operator — routed to, its start unknown, and never a
    receiver of moved work. ``held`` is what it already holds — its running print
    and its own queue — read from the forecast's snapshot; without it, a printer
    that is not ready holds one running print of ``free_after`` seconds (the pure
    tests' farm). A printer that may not receive new work (``routable`` False)
    still finishes what it holds, and its starts still take stagger windows.
    """

    printer_id: int
    model: str  # ``model_key`` of the printer's effective model
    ready: bool
    free_after: float = 0.0
    held: tuple[Held, ...] | None = None
    routable: bool = True
    waiting: float = 0.0  # what holds its next start now
    hold_unknown: bool = False  # held with no forecast release: an HA interlock, a snapshot the router reads otherwise
    gap: float = 0.0
    requires_operator: bool = False
    interval: float = 0.0
    dispatchable: bool = True


class FitSource(Protocol):
    """Where the pure decision asks its feasibility questions (:class:`_FitOracle` in a run)."""

    def home(self, item_id: int, printer_id: int) -> Fit: ...

    def target(self, item_id: int, plate: PlateOption, printer_id: int) -> Fit: ...


@dataclass
class FarmView:
    """The farm as the decision sees it: the machines that may take new work, and who can print what.

    A printer absent from ``machines`` (offline, parked, archived) neither
    vetoes a move, nor dates a home, nor receives — an item whose every home
    machine is absent moves whenever a fit receiver can take it.
    """

    machines: dict[int, Machine]
    fits: FitSource
    allow_compatible: bool = False
    #: ``(job model, machine model)`` → the compatible-models verdict, asked once per
    #: pair for the pass: a pass asks it per job and printer, and the matrix lookup
    #: is not free (review I2 — 1000 jobs × 32 printers blocked the loop for seconds).
    model_verdicts: dict[tuple[str, str], str | None] = field(default_factory=dict, repr=False)
    #: Every other job the router can see — they take printer time too (spec T6).
    tail: tuple[MovableItem, ...] = ()
    sjf: bool = False
    stagger: StaggerPolicy | None = None
    operator_hours: OperatorHours | None = None
    #: The highest position among ALL pending rows: the writer files companions after it (T14).
    #: None = the highest among the jobs here.
    max_position: int | None = None
    #: Replay work units for the whole pass (spec T20); None = unbounded.
    budget: int | None = None


@dataclass(frozen=True)
class Move:
    item_id: int
    line_id: int
    from_model: str
    to_model: str
    plate: PlateOption
    k: int  # prints of ``plate`` that cover what the item covered
    home_finish: float | None
    finish: float
    surplus: int  # k × plate yield − item yield, always < plate yield
    moved_parts: int  # the item's yield — what the move is for
    #: The fit, ready printers the estimate put the k prints on, in the router's
    #: order. An estimate, never a reservation: the router places the rows.
    receivers: tuple[int, ...] = ()


@dataclass
class MovePlan:
    moves: list[Move] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)  # (item_id, reason)
    #: Each candidate's home outcome as the decision read it — ``("known", seconds)``,
    #: ``("unknown", None)`` or ``("none", None)`` (no printer of its own can print it);
    #: absent when the pass had no forecast left for it. A read model, never stored.
    home: dict[int, tuple[str, float | None]] = field(default_factory=dict)


# ---------- the decision ----------


def _model_verdict(job_model: str, machine_model: str, farm: FarmView) -> str | None:
    """``exact`` / ``compatible`` when the router would offer this machine the job's model, else None."""
    if job_model == machine_model:
        return "exact"
    if not farm.allow_compatible:
        return None
    key = (job_model, machine_model)
    if key not in farm.model_verdicts:
        compatible = model_compatibility(job_model, machine_model) == "compatible"
        farm.model_verdicts[key] = "compatible" if compatible else None
    return farm.model_verdicts[key]


def _lane(machine: Machine) -> Lane:
    """A machine as a lane of the replay. Without its own ``held``, a printer that is not ready
    holds one running print of ``free_after`` seconds — the pure tests' farm."""
    held = machine.held
    if held is None:
        held = (Held(float(machine.free_after), started=True),) if not machine.ready and machine.free_after > 0 else ()
    return Lane(
        printer_id=machine.printer_id,
        held=held,
        routable=machine.routable,
        ready=machine.ready,
        waiting=machine.waiting,
        hold_unknown=machine.hold_unknown or not machine.dispatchable,
        gap=machine.gap,
        requires_operator=machine.requires_operator,
        interval=machine.interval,
    )


def _home_job(item: MovableItem, machines: list[Machine], farm: FarmView) -> Job:
    """The row as the router sees it: every printer of its model (or a compatible one, with the
    setting) that may receive work and CAN print it — the shared resolver's yes, nothing else."""
    fits = []
    for machine in machines:
        if not machine.routable:
            continue
        verdict = _model_verdict(item.home_model, machine.model, farm)
        if verdict is None:
            continue
        fit = farm.fits.home(item.item_id, machine.printer_id)
        if fit.verdict == "yes":
            fits.append((machine.printer_id, verdict == "exact", fit.color_matches))
    return Job(
        job_id=item.item_id,
        position=item.position,
        group=item.group,
        # T19: a zero estimate is no estimate — never an instant print.
        seconds=float(item.seconds) if item.seconds is not None and item.seconds > 0 else None,
        fits=tuple(fits),
        been_jumped=item.been_jumped,
        auto_eject=item.auto_eject,
        prep=item.prep,
    )


class _Projection:
    """The pass's baseline — the router's whole tail, replayed — kept per independent part.

    A part (:func:`rebalance_replay.components`) is a set of printers and jobs that cannot
    affect any other; a trial replays only the part its prints land in, and a committed move
    re-plays only the parts whose jobs changed (spec T21.3). The work of every replay counts
    against the pass's one budget (T20).
    """

    def __init__(self, lanes: list[Lane], jobs: dict[int, Job], farm: FarmView):
        self.lanes = {lane.printer_id: lane for lane in lanes}
        self.jobs = dict(jobs)
        self.farm = farm
        self.work = 0
        self._parts: dict[frozenset[int], tuple[frozenset[int], Replay]] = {}
        self._part_of: dict[int, frozenset[int]] = {}
        #: ``(printers, jobs)`` of every printer's part and, with SJF, of every model group's —
        #: jobless parts included.
        self._by_lane: dict[int, tuple[frozenset[int], frozenset[int]]] = {}
        self._by_group: dict[str, tuple[frozenset[int], frozenset[int]]] = {}
        self._replay_changed(set(self.jobs))

    @property
    def exhausted(self) -> bool:
        return self.farm.budget is not None and self.work >= self.farm.budget

    def _replay(
        self, printers: frozenset[int], jobs: list[Job], *, watch: set[int] | None = None, until: float | None = None
    ) -> Replay:
        remaining = None if self.farm.budget is None else max(0, self.farm.budget - self.work)
        try:
            result = replay(
                [self.lanes[pid] for pid in printers],
                jobs,
                sjf=self.farm.sjf,
                stagger=self.farm.stagger,
                operator_hours=self.farm.operator_hours,
                budget=remaining,
                watch=watch,
                until=until,
            )
        except ReplayBudget:
            # What was left is spent: no later replay of this pass is tried, so an earlier
            # candidate is never passed over for a later, cheaper one.
            self.work = self.farm.budget
            raise
        self.work += result.work
        return result

    def _split(self, jobs: dict[int, Job]) -> list[tuple[frozenset[int], frozenset[int]]]:
        return components(list(self.lanes.values()), list(jobs.values()), sjf=self.farm.sjf, stagger=self.farm.stagger)

    def _replay_changed(self, changed: set[int]) -> None:
        parts: dict[frozenset[int], tuple[frozenset[int], Replay]] = {}
        by_lane: dict[int, tuple[frozenset[int], frozenset[int]]] = {}
        by_group: dict[str, tuple[frozenset[int], frozenset[int]]] = {}
        for printers, members in self._split(self.jobs):
            part = (printers, members)
            for pid in printers:
                by_lane[pid] = part
            if self.farm.sjf:
                for job_id in members:
                    group = self.jobs[job_id].group
                    if group is not None:
                        by_group[group] = part
            if not members:
                continue
            kept = self._parts.get(members)
            if kept is not None and kept[0] == printers and not (members & changed):
                parts[members] = kept
            else:
                parts[members] = (printers, self._replay(printers, [self.jobs[j] for j in members]))
        self._parts = parts
        self._part_of = {job_id: members for members in parts for job_id in members}
        self._by_lane = by_lane
        self._by_group = by_group

    def result_of(self, job_id: int) -> Replay:
        return self._parts[self._part_of[job_id]][1]

    def trial(self, jobs: dict[int, Job], transferred: list[int], *, until: float | None = None) -> Replay:
        """Replay only the part of ``jobs`` the transferred prints land in, and only until each
        of them has started — or the clock passed ``until``, the home's finish, after which a
        print not yet started can only end later (spec T20–T21)."""
        wanted = set(transferred)
        # The baseline's parts the moved prints touch — through a printer they fit or, with SJF,
        # their model group — stay closed in the trial: each holds every printer its jobs fit and
        # every printer sharing a start group with them, the moved prints fit only printers among
        # them, and the job taken off its home leaves a part it no longer touches as it was. A
        # union of independent parts replays as each alone (T21.3), so no new split is needed.
        touched: set[tuple[frozenset[int], frozenset[int]]] = set()
        for job_id in transferred:
            job = jobs[job_id]
            touched.update(self._by_lane[pid] for pid, _exact, _colour in job.fits)
            if self.farm.sjf and job.group in self._by_group:
                touched.add(self._by_group[job.group])
        printers = frozenset().union(*(part[0] for part in touched))
        members = frozenset().union(*(part[1] for part in touched)) | wanted
        return self._replay(printers, [jobs[j] for j in members], watch=wanted, until=until)

    def commit(self, jobs: dict[int, Job], changed: set[int]) -> None:
        self.jobs = dict(jobs)
        self._replay_changed(changed)


def _receivers(
    item: MovableItem,
    plate: PlateOption,
    machines: list[Machine],
    farm: FarmView,
    idle: set[int],
    unknown: set[str | None],
) -> list[tuple[int, bool, int]]:
    """The ready printers, free now and not spent by a move of this pass, that can print
    ``plate`` for this job — ``(printer, exact, colour matches)`` in the router's order.

    The pool the trial may put the moved prints on (spec T15): a forecast that needs any other
    printer is no proof. An unknown answer gives no capacity (R8) and is remembered for the reason.
    """
    ranked = []
    for machine in machines:
        # The free-and-ready test first: once the free printers are spent, a pass
        # walks hundreds of jobs that ask nothing more of any printer.
        if not (machine.ready and machine.dispatchable) or machine.printer_id not in idle:
            continue
        verdict = _model_verdict(plate.model, machine.model, farm)
        if verdict is None:
            continue
        fit = farm.fits.target(item.item_id, plate, machine.printer_id)
        if fit.verdict == "yes":
            ranked.append((verdict == "exact", fit.color_matches, -machine.printer_id))
        elif fit.verdict == "unknown":
            unknown.add(fit.reason)
    ranked.sort(reverse=True)
    return [(-pid, exact, colour) for exact, colour, pid in ranked]


def _target_fits(item: MovableItem, plate: PlateOption, machines: list[Machine], farm: FarmView):
    """Every printer that can print ``plate`` for this job — the moved rows as the router will
    see them once the move is made, no longer only the trial's pool."""
    fits = []
    for machine in machines:
        if not machine.routable:
            continue
        verdict = _model_verdict(plate.model, machine.model, farm)
        if verdict is None:
            continue
        fit = farm.fits.target(item.item_id, plate, machine.printer_id)
        if fit.verdict == "yes":
            fits.append((machine.printer_id, verdict == "exact", fit.color_matches))
    return tuple(fits)


def _no_target_reason(unknown: set[str | None]) -> str:
    if not unknown:
        return "no_eligible_target"
    if unknown == {TARGET_SOURCE_UNREADABLE}:
        return "source_unreadable"
    return "eligibility_unknown"


def _lower_bound(k: int, receivers: int, seconds: float) -> float:
    """T21.1: ``k`` prints of ``seconds`` on ``receivers`` printers cannot all end before
    ``ceil(k / receivers)`` of them back to back from now — preparation, staggered starts and
    other work on those printers only add to it. Never the target's baseline clock: removing
    the original from its home can free a start window there earlier."""
    return -(-k // receivers) * seconds


def start_projection(items: list[MovableItem], farm: FarmView) -> _Projection:
    """The baseline of a pass over ``items`` and ``farm.tail`` — built once, so a caller can ask
    the cheap verdicts first (no catalog) and decide the rest on the same replay. May raise
    :class:`ReplayBudget`."""
    machines = sorted(farm.machines.values(), key=lambda m: m.printer_id)
    jobs = {job.item_id: _home_job(job, machines, farm) for job in (*farm.tail, *items)}
    return _Projection([_lane(m) for m in machines], jobs, farm)


def plan_moves(
    items: list[MovableItem],
    options_by_line: dict[int, list[PlateOption]],
    farm: FarmView,
    *,
    cooling: frozenset[int] | set[int] = frozenset(),
    projection: _Projection | None = None,
) -> MovePlan:
    """The decision, item by item, in the order ``items`` arrive (spec rebalance-tail-time r2).

    Pure, given ``farm.fits``. The baseline is the router's whole tail replayed
    (:mod:`rebalance_replay`): the candidates and ``farm.tail`` — every job the router can see —
    dealt out over the printers with what they already hold, in the router's order, with the
    forecast's preparation, plate service, operator hours and staggered starts. A candidate's
    home finish is when THAT job ends there, not when its model first frees.

    Per item: skip if its line is cooling; if the baseline gives it a READY printer now
    (``home_model_idle``); if a ready, free printer of its model cannot yet answer for it
    (``eligibility_unknown``, R8); if its home finish is unknown (``forecast_incomplete`` —
    never 0). Otherwise every alternative plate of another model gets its own pool: the ready
    printers, free now and not spent by an earlier move of this pass, that can print it; per
    model the plan engine's key picks the plate; a plate whose lower bound — ``ceil(k / c) ×
    t``, the pool printing nothing else — already ends after the home finish is out without a
    trial (T21.1). The trial replaces the row as the writer does — the same id and position,
    the plate's model and time — and files ``k − 1`` companions after every pending row (T14);
    the moved rows may land only on the pool. The transferred work ends when the last of them
    does, behind whatever the router puts ahead of them. The item moves to the earliest such
    finish not later than its home finish — or to any when no printer of its model can print it.

    A move spends the printers its prints landed on for the rest of the pass, and the baseline
    becomes the trial's world: the original no longer waits at home, its prints wait on their
    printers as the router will see them. A trial that was not chosen changes nothing. When the
    pass's budget runs out (T20), no later replay is tried: a candidate whose verdict needs a
    trial answers ``forecast_budget``, one the baseline answers alone still gets its verdict.
    A move whose own decision was whole stands even when replaying its consequences runs out —
    then every later candidate answers ``forecast_budget``, the baseline being behind the plan.
    """
    machines = sorted(farm.machines.values(), key=lambda m: m.printer_id)
    lanes = [_lane(m) for m in machines]
    idle = {lane.printer_id for lane in lanes if lane.routable and not lane.held}
    if projection is not None:
        jobs = projection.jobs
    else:
        jobs = {job.item_id: _home_job(job, machines, farm) for job in (*farm.tail, *items)}
    max_position = farm.max_position
    if max_position is None:
        max_position = max((job.position for job in jobs.values()), default=0)
    plan = MovePlan()
    next_id = min([0, *jobs]) - 1
    #: A move's own replay ran out: the move stands — its decision was whole — but the baseline
    #: is behind the plan, so nothing later is read from it.
    stale = False
    for item in items:
        if item.line_id in cooling:
            plan.skipped.append((item.item_id, "cooldown"))
            continue
        if stale:
            plan.skipped.append((item.item_id, "forecast_budget"))
            continue
        try:
            if projection is None:
                projection = _Projection(lanes, jobs, farm)
        except ReplayBudget:
            stale = True
            plan.skipped.append((item.item_id, "forecast_budget"))
            continue
        try:
            base = projection.result_of(item.item_id)
            plan.home[item.item_id] = base.outcome(item.item_id)
            if (
                base.routed_at.get(item.item_id) == 0.0
                and base.ready_when_routed.get(item.item_id)
                and not projection.lanes[base.lane_of[item.item_id]].hold_unknown
            ):
                plan.skipped.append((item.item_id, "home_model_idle"))
                continue
            if any(
                machine.ready
                and machine.dispatchable
                and machine.printer_id in idle
                and _model_verdict(item.home_model, machine.model, farm) is not None
                and farm.fits.home(item.item_id, machine.printer_id).verdict == "unknown"
                for machine in machines
            ):
                plan.skipped.append((item.item_id, "eligibility_unknown"))
                continue
            state, home_finish = base.outcome(item.item_id)
            if state == "unknown":
                plan.skipped.append((item.item_id, "forecast_incomplete"))
                continue
            unknown: set[str | None] = set()
            by_model: dict[str, list[tuple[PlateOption, list[tuple[int, bool, int]]]]] = {}
            for plate in options_by_line.get(item.line_id, ()):
                # A plate without an estimate is never a target: its finish could not be compared.
                if plate.model == item.home_model or plate.yield_parts <= 0 or not plate.seconds or plate.seconds < 0:
                    continue
                pool = _receivers(item, plate, machines, farm, idle, unknown)
                if pool:
                    by_model.setdefault(plate.model, []).append((plate, pool))
            best = None
            incomplete = False
            for model, candidates in by_model.items():
                # The plan engine's own key, among the plates that have a fit receiver:
                # most useful parts per hour, least waste, the shorter print, the lower id.
                plate, pool = min(
                    candidates,
                    key=lambda pair: _pick_key(
                        min(pair[0].yield_parts, item.yield_parts),
                        max(0, pair[0].yield_parts - item.yield_parts),
                        pair[0].seconds,
                        pair[0].plate_id,
                    ),
                )
                k = -(-item.yield_parts // plate.yield_parts)
                if home_finish is not None and _lower_bound(k, len(pool), plate.seconds) > home_finish:
                    continue  # T21.1: even the pool printing nothing else ends later
                if projection.exhausted:
                    # A trial is due and nothing is left to replay it with (T20): no verdict
                    # from a comparison that was not made.
                    raise ReplayBudget()
                seconds = float(plate.seconds)
                converted = Job(
                    job_id=item.item_id,
                    position=item.position,
                    group=plate.model_label,
                    seconds=seconds,
                    fits=tuple(pool),
                    been_jumped=item.been_jumped,
                    auto_eject=item.auto_eject,
                    prep=item.prep,
                )
                companions = [
                    Job(
                        job_id=next_id - i,
                        position=max_position + 1 + i,
                        group=plate.model_label,
                        seconds=seconds,
                        fits=tuple(pool),
                        auto_eject=item.auto_eject,
                        prep=item.prep,
                    )
                    for i in range(k - 1)
                ]
                trial_jobs = {**projection.jobs, item.item_id: converted, **{c.job_id: c for c in companions}}
                moved = [item.item_id, *(c.job_id for c in companions)]
                # A moved print not started by home − (preparation + its time) ends after home.
                latest = None if home_finish is None else home_finish - (item.prep + seconds)
                result = projection.trial(trial_jobs, moved, until=latest)
                outcomes = [result.outcome(job_id) for job_id in moved]
                if any(state == "unknown" for state, _ in outcomes):
                    incomplete = True
                    continue
                if any(state != "known" for state, _ in outcomes):
                    # Cut at the home's finish with a print not started: it can only end later.
                    continue
                finish = max(t for _state, t in outcomes)
                if home_finish is not None and finish > home_finish:
                    continue
                used = tuple(dict.fromkeys(result.lane_of[job_id] for job_id in moved))
                surplus = k * plate.yield_parts - item.yield_parts
                key = (finish, surplus, model)
                if best is None or key < best[0]:
                    best = (key, model, plate, k, finish, surplus, used, converted, companions)
        except ReplayBudget:
            plan.skipped.append((item.item_id, "forecast_budget"))
            continue
        if best is None:
            if incomplete:
                reason = "forecast_incomplete"
            else:
                reason = "no_faster_model" if by_model else _no_target_reason(unknown)
            plan.skipped.append((item.item_id, reason))
            continue
        _key, model, plate, k, finish, surplus, used, converted, companions = best
        plan.moves.append(
            Move(
                item_id=item.item_id,
                line_id=item.line_id,
                from_model=item.home_model,
                to_model=model,
                plate=plate,
                k=k,
                home_finish=home_finish,
                finish=finish,
                surplus=surplus,
                moved_parts=item.yield_parts,
                receivers=used,
            )
        )
        idle -= set(used)
        max_position += k - 1
        next_id -= k - 1
        # The world after the move: the moved rows as the router will see them — every
        # printer that can print the plate, not only the trial's pool (T15).
        widened = _target_fits(item, plate, machines, farm)
        committed = {
            **projection.jobs,
            item.item_id: replace_job(converted, fits=widened),
            **{c.job_id: replace_job(c, fits=widened) for c in companions},
        }
        try:
            projection.commit(committed, {item.item_id, *(c.job_id for c in companions)})
        except ReplayBudget:
            stale = True
    return plan


# ---------- loading what the decision needs ----------


def refusal(item: AutoQueueItem) -> str | None:
    """Why this row may not be moved, or ``None`` when it may (spec §2).

    ``auto_policy`` never answers ``pinned`` for a router row — that mode is the
    print-queue side's explicit AMS mapping — so "pinned" here is a non-empty
    ``filament_overrides`` (slot-bound to the file they were written for) or a
    policy that needs review. ``force_color_match`` is file-independent and
    travels with the move.
    """
    if item.status != "pending" or item.assigned_to_item_id is not None or item.cancelled_at is not None:
        return "already_assigned"
    if item.project_line_id is None:
        return "not_filed"
    if item.scheduled_time is not None:
        return "scheduled"
    if item.manual_start:
        return "staged"
    if item.target_location_id is not None:
        return "located"
    policy = auto_policy(item)
    if policy.mode != "auto" or policy.review_required or policy.filament_overrides:
        return "pinned"
    if item.library_file_id is None:
        return "no_yield"
    return None


def _router_population() -> tuple:
    """The printers the router places on — ``printers_for_item``'s population: active, not
    archived, auto-distribute eligible, the queue not paused by the operator."""
    return (
        Printer.is_active.is_(True),
        Printer.archived.is_(False),
        PrinterQueue.auto_distribute_eligible.is_(True),
        PrinterQueue.is_paused.is_(False),
    )


def _accepts_new_work() -> tuple:
    """A printer that may RECEIVE moved work: the router's population, narrowed to a queue
    ``print_scheduler.check_queue`` would dispatch from — the final check's population too."""
    return (*_router_population(), PrinterQueue.status.not_in(("paused", "error")))


@dataclass(frozen=True)
class PassPrinter:
    """One printer as a rebalancing pass sees it — its facts read ONCE for the whole pass (spec R14)."""

    printer_id: int
    model_name: str  # the effective model, as the resolver and ``model_compatibility`` read it
    key: str  # its ``model_key``
    snapshot: PrinterFeedSnapshot
    ready: bool
    location_id: int | None = None
    dispatchable: bool = True


async def load_printers(db: AsyncSession, busy: set[int], interlocked: set[int]) -> dict[int, PassPrinter]:
    """The online printers the router may place on, with their feed snapshot and readiness.

    The same population ``printers_for_item`` routes over (active, not archived,
    auto-distribute eligible, queue not paused) — the forecast deals the tail out
    over exactly the printers the router does. ``dispatchable`` says whether
    ``print_scheduler.check_queue`` would dispatch from its queue: ⚠️ ``status in
    ("paused", "error")`` is what it refuses, so such a printer is never a receiver
    of moved work, and what the router places there waits for the operator. An
    offline printer is left out whole (spec R7): it neither vetoes, nor dates a
    home, nor receives — not waiting for it to come back.

    ``ready`` = outside the busy set, idle by ``_is_printer_idle`` with the
    plate-clear gate honoured, and not held by a Home Assistant interlock of
    THIS pass's set (C3-1) — the router's own rank: ⚠️ an empty queue on a
    printer still waiting for its plate clear, or behind an alerting sensor, only
    looks free.
    """
    rows = (
        await db.execute(
            select(Printer.id, Printer.model, Printer.location_id, PrinterQueue.status)
            .join(PrinterQueue, PrinterQueue.printer_id == Printer.id)
            .where(*_router_population())
        )
    ).all()
    printers: dict[int, PassPrinter] = {}
    # By printer id, so a printer that somehow answered the query twice (a
    # second queue row of its own) is still one machine.
    for printer_id, model, location_id, queue_status in rows:
        if printer_id in printers:
            continue
        name = printer_manager.effective_model_for(printer_id, model)
        key = model_key(name)
        if key is None:
            continue
        snapshot = printer_manager.get_feed_snapshot(printer_id)
        if not snapshot.connected:
            continue
        ready = (
            printer_id not in busy
            and printer_id not in interlocked
            and scheduler._is_printer_idle(printer_id, require_plate_clear=True)
        )
        dispatchable = queue_status not in ("paused", "error")
        printers[printer_id] = PassPrinter(printer_id, name, key, snapshot, ready, location_id, dispatchable)
    return printers


def _content_key(req: PrintRequirements) -> str:
    """What a requirement says, without where it was read from — the staged copy is compared with it."""
    return hashlib.sha256(
        json.dumps(
            {
                "model": req.model,
                "plate": req.resolved_plate_id,
                "filaments": req.used_filaments,
                "nozzles": req.nozzle_constraints,
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class _Job:
    requirements: PrintRequirements
    key: tuple  # (source identity, content) — the requirements' identity in the verdict cache
    policy: RoutingPolicy
    policy_fingerprint: str
    require_previous_success: bool
    #: The printers the job may run on at all — its location's subtree and its order's
    #: resources, as ``printers_for_item`` gates them; None = no such limit.
    allowed: frozenset[int] | None = None


def fit_of(result) -> Fit:
    """A ``RoutingResult`` as the decision reads it."""
    if result.status == "compatible":
        return Fit("yes", None, result.plan.color_matches)
    return Fit("no" if result.status == "incompatible" else "unknown", result.reason)


class _FitOracle:
    """«Can printer P print job J now» for one pass — the shared resolver over frozen snapshots.

    ⚠️ **Never ``find_eligible_printer``.** The router writes the plate and the
    model onto the row it routes, and answers nothing about a busy printer: it
    checks busy BEFORE it resolves, so its waiting facts cannot say whether a
    busy home could print the job (spec O2). This asks the same pieces the router
    asks, in the router's order — the model, ``require_previous_success``, then
    ``resolve_filament_routing`` under the job's own ``auto_policy`` — of every
    printer, without touching the row, and the router is left exactly as it was.

    Verdicts are cached for the pass by the requirements' identity, the policy's
    own fingerprint (``RoutingPolicy.fingerprint``: ``feed_signature`` does not
    name the policy — one snapshot under strict colour on and off answers two
    different questions), the printer, its ``feed_signature``, the exactness of
    its model and ``prefer_lowest`` — so 358 copies of one job cost one resolve
    per printer. ``require_previous_success`` is the JOB's condition and is
    applied outside the cache: one job's flag is never another's answer.
    """

    def __init__(self, printers: dict[int, PassPrinter], *, prefer_lowest: bool, previous_ok: dict[int, bool]):
        self._printers = printers
        self._prefer_lowest = prefer_lowest
        self._previous_ok = previous_ok
        self._jobs: dict[int, _Job] = {}
        self._plates: dict[int, tuple[PrintRequirements, tuple] | None] = {}
        #: plate id → the revision of its file the decision read: the library row's path and the
        #: original's size and ``st_mtime_ns`` (:func:`_target_revision`).
        self._revisions: dict[int, tuple] = {}
        self._verdicts: dict[tuple, Fit] = {}
        self._signatures: dict[tuple[int, bool], tuple] = {}
        #: ``id(requirements)`` → its cache key: copies of one job share one read (R14),
        #: so the key is computed once per distinct source, not once per row.
        self._req_keys: dict[int, tuple] = {}
        self._models: dict[tuple[str | None, int], str] = {}
        self.resolves = 0
        self.hits = 0

    def add_job(
        self,
        item: AutoQueueItem,
        requirements: PrintRequirements,
        policy: RoutingPolicy,
        allowed: frozenset[int] | None = None,
    ) -> None:
        key = self._req_keys.get(id(requirements))
        if key is None:
            key = self._req_keys[id(requirements)] = (requirements.source_identity, _content_key(requirements))
        self._jobs[item.id] = _Job(
            requirements, key, policy, policy.fingerprint, bool(item.require_previous_success), allowed
        )

    def add_plate(self, plate_id: int, requirements: PrintRequirements | None, *, file_path: str | None = None) -> None:
        """An alternative plate's requirements, read from its library file; ``None`` = unreadable."""
        if requirements is None:
            self._plates[plate_id] = None
        else:
            key = (requirements.source_identity, _content_key(requirements))
            self._plates[plate_id] = (requirements, key)
            identity = requirements.source_identity
            if identity is not None:
                self._revisions[plate_id] = (file_path, identity.size, identity.mtime_ns)

    def plate_content(self, plate_id: int) -> str | None:
        entry = self._plates.get(plate_id)
        return None if entry is None else entry[1][1]

    def plate_revision(self, plate_id: int) -> tuple | None:
        return self._revisions.get(plate_id)

    def policy_fingerprint(self, item_id: int) -> str:
        return self._jobs[item_id].policy_fingerprint

    def home(self, item_id: int, printer_id: int) -> Fit:
        job = self._jobs[item_id]
        return self._fit(job, job.requirements, job.key, printer_id)

    def target(self, item_id: int, plate: PlateOption, printer_id: int) -> Fit:
        entry = self._plates.get(plate.plate_id)
        if entry is None:
            return Fit("unknown", TARGET_SOURCE_UNREADABLE)
        requirements, key = entry
        return self._fit(self._jobs[item_id], requirements, key, printer_id)

    def _fit(self, job: _Job, requirements: PrintRequirements, req_key: tuple, printer_id: int) -> Fit:
        if job.allowed is not None and printer_id not in job.allowed:
            return Fit("no", "not_allowed")
        if job.require_previous_success and not self._previous_ok.get(printer_id, False):
            return Fit("no", "previous_print_failed")
        printer = self._printers[printer_id]
        verdict = self._models.get((requirements.model, printer_id))
        if verdict is None:
            verdict = self._models[(requirements.model, printer_id)] = model_compatibility(
                requirements.model, printer.model_name
            )
        if verdict not in ("exact", "compatible"):
            return Fit("no", "model_mismatch")
        allow_base = job.policy.allow_base_material_match
        signature = self._signatures.get((printer_id, allow_base))
        if signature is None:
            signature = self._signatures[(printer_id, allow_base)] = feed_signature(job.policy, printer.snapshot)
        key = (req_key, job.policy_fingerprint, printer_id, signature, verdict == "exact", self._prefer_lowest)
        fit = self._verdicts.get(key)
        if fit is not None:
            self.hits += 1
            return fit
        self.resolves += 1
        fit = self._verdicts[key] = fit_of(
            resolve_filament_routing(
                requirements,
                job.policy,
                printer.snapshot,
                exact_model=verdict == "exact",
                prefer_lowest=self._prefer_lowest,
            )
        )
        return fit


@dataclass
class LineCatalog:
    """What the decision knows about the lines of some orders.

    ``options_by_line`` are the candidate plates the plan would consider —
    sliced, material accepted, a known model, a positive yield for the line —
    and :meth:`yield_of` reads a queued row's yield through the plan engine's
    own ``recipes_for_row``, so a rebalanced row and the plan on screen resolve
    the same plate to the same recipe. ⚠️ ``plate_id`` on a queue row is the
    plate INDEX, not a ``ProductPlate.id``.
    """

    options_by_line: dict[int, list[PlateOption]]
    counted_by_line: dict[int, set[int]]
    product_by_line: dict[int, int]
    by_plate: dict[tuple[int, int], dict[int, PlateRecipe]]
    by_file: dict[int, dict[int, PlateRecipe]]

    def yield_of(self, line_id: int, library_file_id: int, plate_id: int | None) -> int:
        product_id = self.product_by_line.get(line_id)
        if product_id is None:
            return 0
        recipe = recipes_for_row(self.by_plate, self.by_file, library_file_id, plate_id or 0).get(product_id)
        if recipe is None:
            return 0
        return sum(line_yield(recipe, self.counted_by_line.get(line_id) or set()).values())


async def load_line_catalog(db: AsyncSession, project_ids: list[int]) -> LineCatalog:
    """One load for every line of ``project_ids`` — the plan engine's own loaders, the plan engine's own filter."""
    contexts = await batch_contexts(db, project_ids) if project_ids else []
    figures_by_project = {ctx.project.id: attribute(ctx)[0] for ctx in contexts}
    products_by_id = {pid: product for ctx in contexts for pid, product in ctx.products_by_id.items()}
    recipes_by_product = await recipes_for_products(db, products_by_id.values()) if products_by_id else {}
    counted_by_line = counted_parts_by_line(figures_by_project)
    by_plate, by_file = plate_recipe_index(recipes_by_product)
    options_by_line: dict[int, list[PlateOption]] = {}
    product_by_line: dict[int, int] = {}
    for ctx in contexts:
        for line in ctx.lines:
            product_by_line[line.id] = line.product_id
            counted = counted_by_line.get(line.id) or set()
            options: list[PlateOption] = []
            for plate, _file, recipe in recipes_by_product.get(line.product_id) or []:
                # The filter ``plan_lines`` applies: sliced, and the line's material accepted.
                if not recipe.sliced or not line_accepts_materials(line, recipe.materials):
                    continue
                key = model_key(recipe.printer_model)
                if key is None:
                    continue
                parts = sum(line_yield(recipe, counted).values())
                if parts <= 0:
                    continue
                options.append(
                    PlateOption(
                        plate_id=plate.id,
                        library_file_id=plate.library_file_id,
                        plate_index=plate.plate_index,
                        model=key,
                        model_label=recipe.printer_model or "",
                        yield_parts=parts,
                        seconds=estimate_seconds(recipe),
                    )
                )
            options_by_line[line.id] = options
    return LineCatalog(options_by_line, counted_by_line, product_by_line, by_plate, by_file)


async def _compatible_models_allowed(db: AsyncSession) -> bool:
    """The farm's «compatible models» setting — the router's own condition for a compatible printer."""
    setting = await db.scalar(select(Settings.value).where(Settings.key == "auto_queue_compatible_models"))
    return isinstance(setting, str) and setting.lower() == "true"


def _utcnow() -> datetime:
    """Naive UTC — what the DateTime columns and ``load_snapshot`` speak."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


async def cooling_lines(db: AsyncSession, line_ids: set[int], now: datetime) -> set[int]:
    """Lines moved within ``REBALANCE_COOLDOWN_SECONDS`` — ``MAX(rebalanced_at)`` over each line's rows."""
    if not line_ids:
        return set()
    rows = (
        await db.execute(
            select(AutoQueueItem.project_line_id, func.max(AutoQueueItem.rebalanced_at))
            .where(AutoQueueItem.project_line_id.in_(line_ids), AutoQueueItem.rebalanced_at.is_not(None))
            .group_by(AutoQueueItem.project_line_id)
        )
    ).all()
    out: set[int] = set()
    for line_id, last in rows:
        if last is None:
            continue
        if isinstance(last, str):  # SQLite hands MAX() of a DateTime back as text
            last = datetime.fromisoformat(last)
        if (now - _naive(last)).total_seconds() < REBALANCE_COOLDOWN_SECONDS:
            out.add(line_id)
    return out


async def _candidate_rows(
    db: AsyncSession, *, line_ids: list[int] | None, item_ids: list[int] | None
) -> list[AutoQueueItem]:
    """The rows a run looks at. Named ids come back whatever their state, so
    :func:`refusal` can say why; otherwise the SQL already excludes what the
    rules exclude, and ``refusal`` is a cheap second opinion."""
    stmt = select(AutoQueueItem)
    if item_ids is not None:
        stmt = stmt.where(AutoQueueItem.id.in_(item_ids))
    else:
        stmt = stmt.where(
            *awaiting_auto_row_conditions(),
            AutoQueueItem.cancelled_at.is_(None),
            AutoQueueItem.project_line_id.is_not(None),
            AutoQueueItem.scheduled_time.is_(None),
            AutoQueueItem.manual_start.is_(False),
            AutoQueueItem.target_location_id.is_(None),
            AutoQueueItem.library_file_id.is_not(None),
        )
    if line_ids is not None:
        stmt = stmt.where(AutoQueueItem.project_line_id.in_(line_ids))
    return list((await db.execute(stmt.order_by(AutoQueueItem.position, AutoQueueItem.id))).scalars().all())


# ---------- the run ----------


@dataclass
class RebalanceResult:
    converted: int = 0
    created: int = 0
    cancelled: int = 0  # always 0 in this design; see ``RebalanceOut``
    moved_parts: int = 0
    skipped: list[tuple[int, str]] = field(default_factory=list)

    def as_response(self) -> RebalanceOut:
        return RebalanceOut(
            converted=self.converted,
            created=self.created,
            cancelled=self.cancelled,
            moved_parts=self.moved_parts,
            skipped=[RebalanceSkipped(item_id=item_id, reason=reason) for item_id, reason in self.skipped],
        )


def _tail_item(row: AutoQueueItem, home: str, prep: float) -> MovableItem:
    """A pending row as a job of the forecast — its own time, place in the router's order and
    SJF group; ``yield_parts`` is the decision's business and filled in for candidates."""
    return MovableItem(
        item_id=row.id,
        line_id=row.project_line_id,
        home_model=home,
        yield_parts=0,
        seconds=row.print_time_seconds,
        position=row.position or 0,
        group=row.target_model,
        been_jumped=bool(row.been_jumped),
        auto_eject=bool(row.auto_eject),
        prep=float(prep),
    )


def _machines(
    snapshot: FarmSnapshot, printers: dict[int, PassPrinter], busy: set[int], interlocked: set[int]
) -> dict[int, Machine]:
    """Every printer of the forecast's snapshot as the replay's lane — what it holds and what
    holds it, read exactly as ``farm_forecast._initial_state`` reads them.

    It may receive work only when the pass found it routable (``load_printers``: the router's
    population, online); every other one still finishes what it holds and still takes stagger
    windows. A routable lane is free now exactly when the router may place on it now — outside
    the busy set (spec T-A25). What has no forecast release is unknown, never 0 (spec T12, T19):
    an HA interlock; a printer busy for the router with nothing timed to show for it (a claim
    before its archive exists, a print past its estimate, a row of an order that may not print
    here); a printer the router counts free while the snapshot says it holds work (an
    inconsistent snapshot, T-A05) — routed to now, its start unknown.
    """
    from backend.app.services.order_resources import permits

    machines: dict[int, Machine] = {}
    for state in snapshot.printers:
        pid = state.printer_id
        passing = printers.get(pid)
        held: list[Held] = []
        for row in state.queued:
            # A row of an order that may not use this printer is never dispatched here.
            if not (row.started or permits(snapshot.order_resources.get(row.order_id), pid, state.model)):
                continue
            seconds = None if row.seconds is None else float(row.seconds)
            if seconds is not None and seconds <= 0:
                if row.started and (pid not in busy or passing is None):
                    continue  # past its estimate: over, unless the router still counts it printing
                seconds = None  # past its estimate, or estimated at nothing: it ends, nobody knows when
            held.append(Held(seconds, prep=float(row.prep_seconds), started=row.started, auto_eject=row.auto_eject))
        hold_unknown = pid in interlocked
        if passing is not None and pid in busy and not held:
            held = [Held(None, started=True)]
        elif passing is not None and held and pid not in busy:
            held = []
            hold_unknown = True
        gap = max(0.0, float(state.plate_clear_seconds))
        waiting = max(0.0, float(state.waiting_seconds))
        if snapshot.operator_hours and state.awaiting_operator:
            waiting = snapshot.operator_hours.service_finish(max(0.0, waiting - gap), gap)
        machines[pid] = Machine(
            printer_id=pid,
            model=passing.key if passing else (model_key(state.model) or ""),
            ready=bool(passing and passing.ready),
            held=tuple(held),
            routable=passing is not None,
            waiting=waiting,
            hold_unknown=hold_unknown,
            gap=gap,
            requires_operator=state.requires_operator or gap > 0,
            interval=float(state.stagger_interval_seconds or 0),
            dispatchable=bool(passing is None or passing.dispatchable),
        )
    return machines


_collector_lock = threading.Lock()
_collector_holds = 0
_collector_was_enabled = False


@contextlib.contextmanager
def _collector_paused():
    """The cyclic collector waits while a pass decides (spec T22, measured 2026-10-28).

    A full collection stops every thread, the event loop's too, for as long as the whole heap
    takes — and a decision's short-lived replays, promoted by the young collections they set
    off, were what started one in the middle of a pass. The decision makes no reference
    cycles, so its garbage goes with its references and nothing waits for the collector.
    Counted: two passes deciding at once hand it back once, when the last ends, and a
    collector somebody else switched off stays off.
    """
    global _collector_holds, _collector_was_enabled
    with _collector_lock:
        if _collector_holds == 0:
            _collector_was_enabled = gc.isenabled()
            gc.disable()
        _collector_holds += 1
    try:
        yield
    finally:
        with _collector_lock:
            _collector_holds -= 1
            if _collector_holds == 0 and _collector_was_enabled:
                gc.enable()


async def _decide(fn, *args, **kwargs):
    """One step of the decision — CPU work over plain data — in a worker thread, off the
    event loop, with the cyclic collector paused (spec T21–T22). A cancelled pass stops
    using the answer; the thread finishes within the pass's budget."""

    def run():
        with _collector_paused():
            return fn(*args, **kwargs)

    return await asyncio.to_thread(run)


@dataclass
class _PassContext:
    """What one pass reads of the farm before it reads a single job — once, for the whole pass,
    never kept for the next one (spec rebalance-tail-time r2, T21)."""

    printers: dict[int, PassPrinter]
    busy: set[int]
    interlocked: set[int]
    resources: dict[int, object]
    allow_compatible: bool
    prefer_lowest: bool
    sjf: bool
    cache: PrintRequirementsCache
    #: One read per distinct source for the whole pass (spec R14): 358 copies of one
    #: job are one stat, one parse and one family lookup, not 358 of each.
    read: dict[tuple, PrintRequirements] = field(default_factory=dict)

    async def requirements_of(self, db: AsyncSession, row: AutoQueueItem) -> PrintRequirements:
        source = _source_key(row)
        found = self.read.get(source)
        if found is None:
            found = self.read[source] = await read_item_requirements(db, row, self.cache)
        return found


async def _order_resources(db: AsyncSession) -> dict[int, object]:
    """Every order's printer-resources policy, by order id — only the orders that name one."""
    from backend.app.models.project import Project

    return dict(
        (
            await db.execute(
                select(Project.id, Project.printer_resources).where(Project.printer_resources.is_not(None))
            )
        ).all()
    )


async def _pass_context(
    db: AsyncSession,
    *,
    resources: dict[int, object],
    busy: set[int] | None,
    interlocked: set[int] | None,
    cache: PrintRequirementsCache | None,
) -> _PassContext:
    """The pass's printers and settings. ``busy`` / ``interlocked`` are the tick's sets when it
    hands them in — an empty set is a snapshot like any other, not «unknown» — and are read
    freshly otherwise: the interlocks once, a failed read the caller's error, never «nothing is
    interlocked»."""
    if busy is None:
        busy = await busy_printer_ids(db)
    if interlocked is None:
        from backend.app.services.ha_sensor_manager import ha_sensor_manager

        interlocked = set(await ha_sensor_manager.blocked_printers(db))
    interlocked = set(interlocked)
    return _PassContext(
        printers=await load_printers(db, busy, interlocked),
        busy=busy,
        interlocked=interlocked,
        resources=resources,
        allow_compatible=await _compatible_models_allowed(db),
        prefer_lowest=await scheduler._get_bool_setting(db, "prefer_lowest_filament", default=True),
        sjf=await scheduler._get_bool_setting(db, SJF_SETTING_KEY),
        cache=cache if cache is not None else PrintRequirementsCache(),
    )


class _PassGroups:
    """A stagger resolver asked once per printer for the whole pass: every replay and every
    ``_admit`` asks it again on each start, and its answers cannot change within a pass."""

    def __init__(self, resolver):
        self._resolver = resolver
        self._groups: dict[int, frozenset] = {}
        self._caps: dict[tuple, int] = {}

    def groups_for(self, printer_id: int) -> frozenset:
        found = self._groups.get(printer_id)
        if found is None:
            found = self._groups[printer_id] = frozenset(self._resolver.groups_for(printer_id))
        return found

    def cap_for(self, key, global_cap: int) -> int:
        found = self._caps.get((key, global_cap))
        if found is None:
            found = self._caps[(key, global_cap)] = self._resolver.cap_for(key, global_cap)
        return found


@dataclass
class _Forecast:
    """The pass's world as plain data — the farm view (its lanes, the router's tail, the frozen
    fit oracle) and the candidates as jobs. Everything the decision's worker thread reads, and
    nothing that talks to a session or a live client (spec T21)."""

    farm: FarmView
    oracle: _FitOracle
    probes: list[MovableItem]
    #: candidate id → the row as the decision read it (:func:`_row_identity`).
    evaluated: dict[int, tuple]


async def _load_forecast(
    db: AsyncSession, ctx: _PassContext, candidates: list[tuple[AutoQueueItem, str]], now: datetime
) -> _Forecast:
    """The forecast's world (spec rebalance-tail-time r2): what every printer already holds, and
    every job the router can see — the candidates among them, and all the others, each with the
    printers it may be routed to. Reads the database and the sources; returns plain data."""
    from backend.app.services.order_resources import constrained, permits

    snapshot = await load_snapshot(db, now)
    router_rows = list(
        (
            await db.execute(
                select(AutoQueueItem)
                .where(*router_view_conditions(datetime.now(timezone.utc)))
                .order_by(AutoQueueItem.position, AutoQueueItem.id)
            )
        )
        .scalars()
        .all()
    )
    candidate_ids = {row.id for row, _home in candidates}
    tail_rows = [row for row in router_rows if row.id not in candidate_ids]
    requirements_by_row = {row.id: await ctx.requirements_of(db, row) for row in tail_rows}
    for row, _home in candidates:
        requirements_by_row[row.id] = await ctx.requirements_of(db, row)
    previous_ok: dict[int, bool] = {}
    if any(row.require_previous_success for row in (*tail_rows, *(row for row, _home in candidates))):
        # A printer's fact, read once; whether it applies is each job's own flag.
        previous_ok = {pid: await scheduler.previous_print_succeeded(db, pid) for pid in sorted(ctx.printers)}
    oracle = _FitOracle(ctx.printers, prefer_lowest=ctx.prefer_lowest, previous_ok=previous_ok)
    tree = await load_tree(db) if any(row.target_location_id for row in tail_rows) else {}
    line_orders = dict(
        (
            await db.execute(
                select(ProjectLine.id, ProjectLine.project_id).where(
                    ProjectLine.id.in_({row.project_line_id for row in tail_rows if row.project_line_id is not None})
                )
            )
        ).all()
    )

    def allowed(row: AutoQueueItem) -> frozenset[int] | None:
        """``printers_for_item``'s two gates: the location subtree and the order's resources."""
        gate: set[int] | None = None
        if row.target_location_id:
            places = set(subtree_ids(tree, row.target_location_id))
            gate = {pid for pid, p in ctx.printers.items() if p.location_id in places}
        policy = ctx.resources.get(row.project_id or line_orders.get(row.project_line_id))
        if constrained(policy):
            permitted = {pid for pid, p in ctx.printers.items() if permits(policy, pid, p.model_name)}
            gate = permitted if gate is None else gate & permitted
        return None if gate is None else frozenset(gate)

    tail: list[MovableItem] = []
    for row in tail_rows:
        requirements = requirements_by_row[row.id]
        if requirements.status != "ok":
            # The router fails or waits on an unreadable source: it never takes a printer.
            home = ""
        else:
            oracle.add_job(row, requirements, auto_policy(row), allowed(row))
            home = model_key(row.target_model or requirements.model) or ""
        tail.append(_tail_item(row, home, snapshot.prep_seconds))
    evaluated: dict[int, tuple] = {}
    for row, _home in candidates:
        oracle.add_job(row, requirements_by_row[row.id], auto_policy(row))
        evaluated[row.id] = _row_identity(row)
    max_position = await db.scalar(
        select(func.coalesce(func.max(AutoQueueItem.position), 0)).where(AutoQueueItem.status == "pending")
    )
    stagger = snapshot.stagger
    if stagger is not None:
        stagger = replace_job(stagger, resolver=_PassGroups(stagger.resolver))
    farm = FarmView(
        machines=_machines(snapshot, ctx.printers, ctx.busy, ctx.interlocked),
        fits=oracle,
        allow_compatible=ctx.allow_compatible,
        tail=tuple(tail),
        sjf=ctx.sjf,
        stagger=stagger,
        operator_hours=snapshot.operator_hours,
        max_position=int(max_position or 0),
        budget=REBALANCE_WORK_BUDGET,
    )
    probes = [_tail_item(row, home, snapshot.prep_seconds) for row, home in candidates]
    return _Forecast(farm=farm, oracle=oracle, probes=probes, evaluated=evaluated)


async def rebalance(
    db: AsyncSession,
    *,
    line_ids: list[int] | None = None,
    item_ids: list[int] | None = None,
    force: bool = False,
    current_user: User | None = None,
    busy_printers: set[int] | None = None,
    interlocked_printers: set[int] | None = None,
    requirements_cache: PrintRequirementsCache | None = None,
    now: datetime | None = None,
) -> RebalanceResult:
    """Run the procedure (spec §3) over the whole router, one line, or named items.

    ``force`` ignores the per-line cooldown — both manual routes pass it; the
    tick does not. It sets aside nothing else: who can print a job is asked the
    same way on every door. ``busy_printers`` and ``interlocked_printers`` let
    the tick hand in the sets its router pass just used — an empty set is a
    snapshot like any other, not «unknown»; anybody else gets them freshly, the
    interlocks read once. ``requirements_cache`` lets the tick share the parses
    its router pass already paid for. Every conversion and creation is logged at
    INFO with the line, the models, ``k`` and the two finish times compared.
    """
    now = now or _utcnow()
    result = RebalanceResult()
    rows = await _candidate_rows(db, line_ids=line_ids, item_ids=item_ids)
    if item_ids is not None:
        found = {row.id for row in rows}
        # ``dict.fromkeys``: a body that names the same unknown id twice is
        # refused once, not twice — ``skipped`` is one verdict per item.
        result.skipped.extend((item_id, "not_found") for item_id in dict.fromkeys(item_ids) if item_id not in found)
    movable_rows: list[AutoQueueItem] = []
    from backend.app.services.order_resources import constrained

    resources = await _order_resources(db)
    resource_orders = dict(
        (
            await db.execute(
                select(ProjectLine.id, ProjectLine.project_id).where(
                    ProjectLine.id.in_({row.project_line_id for row in rows if row.project_line_id is not None})
                )
            )
        ).all()
    )
    for row in rows:
        if constrained(resources.get(resource_orders.get(row.project_line_id, row.project_id))):
            # A constrained order uses its reviewed fleet replan, not the
            # model-wide idle counters of the opportunistic rebalancer.
            result.skipped.append((row.id, "order_resources"))
            continue
        why = refusal(row)
        if why:
            result.skipped.append((row.id, why))
        else:
            movable_rows.append(row)
    if not movable_rows:
        return result

    ctx = await _pass_context(
        db, resources=resources, busy=busy_printers, interlocked=interlocked_printers, cache=requirements_cache
    )
    if not any(printer.ready and printer.dispatchable for printer in ctx.printers.values()):
        # No printer is free: nothing can receive, and nothing can hold a job at
        # home either — answered without reading a single file.
        result.skipped.extend((row.id, "no_eligible_target") for row in movable_rows)
        return result

    cooling = set() if force else await cooling_lines(db, {row.project_line_id for row in movable_rows}, now)
    candidates: list[tuple[AutoQueueItem, str]] = []
    for row in movable_rows:
        if row.project_line_id in cooling:
            result.skipped.append((row.id, "cooldown"))
            continue
        if not force and (row.rebalanced_at is not None or row.rebalanced_from_model):
            # T23 (the owner's decision): one automatic move per row, then by hand only.
            # The row stays in the forecast's tail and routes as any other.
            result.skipped.append((row.id, "already_rebalanced_auto"))
            continue
        home = model_key(row.target_model)
        if home is None:
            result.skipped.append((row.id, "no_yield"))
            continue
        # The job's ACCEPTED source — its captured bytes, never the library's
        # current copy (spec R4). Unreadable: this job alone stays (R7).
        requirements = await ctx.requirements_of(db, row)
        if requirements.status != "ok":
            result.skipped.append((row.id, _source_skip(requirements.reason)))
            continue
        candidates.append((row, home))
    if not candidates:
        return result

    forecast = await _load_forecast(db, ctx, candidates, now)
    farm = forecast.farm
    try:
        projection = await _decide(start_projection, forecast.probes, farm)
    except ReplayBudget:
        result.skipped.extend((row.id, "forecast_budget") for row, _home in candidates)
        return result
    # The cheap verdicts BEFORE the catalog: everything below this loads every
    # archive of every affected order, and a pass where every candidate has a
    # ready home printer that takes it now, or no known home finish, must not pay
    # for it. With no plates, ``plan_moves`` answers only those — and
    # ``no_eligible_target`` for everyone else, who goes on.
    early = await _decide(plan_moves, forecast.probes, {}, farm, projection=projection)
    held = {item_id: reason for item_id, reason in early.skipped if reason != "no_eligible_target"}
    result.skipped.extend((row.id, held[row.id]) for row, _home in candidates if row.id in held)
    candidates = [(row, home) for row, home in candidates if row.id not in held]
    if not candidates:
        return result

    line_projects: dict[int, int] = dict(
        (
            await db.execute(
                select(ProjectLine.id, ProjectLine.project_id).where(
                    ProjectLine.id.in_({row.project_line_id for row, _home in candidates})
                )
            )
        ).all()
    )
    catalog = await load_line_catalog(db, sorted(set(line_projects.values())))
    homes = {row.id: home for row, home in candidates}
    await _read_alternative_plates(db, catalog, [row for row, _home in candidates], homes, ctx.cache, forecast.oracle)
    rank = {project_id: i for i, project_id in enumerate(await rank_active_orders(db))}

    probe_of = {probe.item_id: probe for probe in forecast.probes}
    keyed: list[tuple[tuple[int, int, int], MovableItem]] = []
    for row, _home in candidates:
        parts = catalog.yield_of(row.project_line_id, row.library_file_id, row.plate_id)
        if parts <= 0:
            result.skipped.append((row.id, "no_yield"))
            continue
        order_rank = rank.get(line_projects.get(row.project_line_id), len(rank))
        keyed.append(((order_rank, row.position, row.id), replace_job(probe_of[row.id], yield_parts=parts)))
    keyed.sort(key=lambda pair: pair[0])
    # ``cooling`` is handed in although no cooling row reached here: the pure
    # function's own guarantee should not depend on a caller's pre-filter.
    plan = await _decide(
        plan_moves,
        [item for _key, item in keyed],
        catalog.options_by_line,
        farm,
        cooling=cooling,
        projection=projection,
    )
    result.skipped.extend(plan.skipped)
    logger.debug(
        "Rebalance: %d candidate(s), %d move(s); fit resolves %d, cache hits %d, replay work %d",
        len(keyed),
        len(plan.moves),
        forecast.oracle.resolves,
        forecast.oracle.hits,
        projection.work,
    )

    by_id = {row.id: row for row, _home in candidates}
    recheck = _Recheck(
        interlocked=set(ctx.interlocked),
        prefer_lowest=ctx.prefer_lowest,
        oracle=forecast.oracle,
        rows=forecast.evaluated,
        projects=line_projects,
    )
    for index, move in enumerate(plan.moves):
        before = result.converted
        await _apply(
            db,
            by_id[move.item_id],
            move,
            now=now,
            cache=ctx.cache,
            current_user=current_user,
            result=result,
            recheck=recheck,
        )
        if result.converted == before:
            # T17: the rest of the plan was decided in a world where this move happened.
            # It is not rebuilt in this call — the next one reads the new farm.
            result.skipped.extend(
                (later.item_id, "plan_invalidated_after_apply_failure") for later in plan.moves[index + 1 :]
            )
            break
    return result


def _source_key(item: AutoQueueItem) -> tuple:
    """What a job's requirements are read from: two rows with the same key read the same bytes.

    A captured job reads its blob through its own snapshot (``item_descriptor``),
    so the snapshot is part of the key; a legacy row reads its original rows.
    """
    if item.queue_source_id:
        snapshot = json.dumps(item.source_snapshot, sort_keys=True, default=str)
        return ("blob", item.queue_source_id, item.plate_id, snapshot)
    return ("original", item.archive_id, item.library_file_id, item.plate_id)


def _row_identity(item: AutoQueueItem) -> tuple:
    """The row as the decision read it: what another run's move of the same job would change
    (source, model, move stamp), and the rest of what the decision took from it — the line, the
    job's own time (the home's finish) and its previous-success condition (who may receive it)."""
    return (
        _source_key(item),
        item.target_model,
        item.rebalanced_at,
        item.project_line_id,
        item.print_time_seconds,
        bool(item.require_previous_success),
    )


def _source_skip(reason: str | None) -> str:
    """How an unreadable HOME source is reported — the capture buckets' own split (spec R17)."""
    return "source_copy_busy" if reason == "source_check_busy" else "source_unreadable"


async def _read_alternative_plates(
    db: AsyncSession,
    catalog: LineCatalog,
    candidates: list[AutoQueueItem],
    homes: dict[int, str],
    cache: PrintRequirementsCache,
    oracle: _FitOracle,
) -> None:
    """The strict requirements of every alternative plate the candidates could move to.

    Read from the plate's library file — the bytes a move would capture — once
    per plate, enriched the way the router reads the moved row afterwards. An
    unreadable or trashed file is an unknown target (no capacity), never a guess.
    A plate of the job's own model is never a target (:func:`plan_moves`), so it
    is not read for that job.
    """
    plates = {
        option.plate_id: option
        for row in candidates
        for option in catalog.options_by_line.get(row.project_line_id, ())
        if option.seconds is not None and option.yield_parts > 0 and option.model != homes[row.id]
    }
    if not plates:
        return
    files = {
        file.id: file
        for file in (
            await db.execute(
                LibraryFile.active().where(LibraryFile.id.in_({option.library_file_id for option in plates.values()}))
            )
        )
        .scalars()
        .all()
    }
    for plate_id, option in plates.items():
        file = files.get(option.library_file_id)
        if file is None:
            oracle.add_plate(plate_id, None)
            continue
        requirements = await cache.read(resolve_source_path(None, file), option.plate_index)
        if requirements.status != "ok":
            oracle.add_plate(plate_id, None)
            continue
        oracle.add_plate(plate_id, await enrich_family_filament_types(db, requirements), file_path=file.file_path)


@dataclass
class _Recheck:
    """What the final check before a move compares against — the pass's own facts (spec R15)."""

    interlocked: set[int]
    prefer_lowest: bool
    oracle: _FitOracle
    rows: dict[int, tuple]  # item id → :func:`_row_identity` as the decision read it
    projects: dict[int, int]  # line id → its order, whose catalog the move's numbers came from


def _row_changed(fresh: AutoQueueItem | None, item_id: int, recheck: _Recheck) -> str | None:
    """Why the row is no longer the one the move was decided on — ``None`` when it still is."""
    if fresh is None:
        return "not_found"
    why = refusal(fresh)
    if why:
        return why
    if _row_identity(fresh) != recheck.rows.get(item_id):
        return "eligibility_changed"
    if auto_policy(fresh).fingerprint != recheck.oracle.policy_fingerprint(item_id):
        return "eligibility_changed"
    return None


def _target_revision(file: LibraryFile, staged: StagedSource) -> tuple:
    """Which revision of the target file the copy is: the library row's path and the original's
    size and ``st_mtime_ns`` as the copy proved them unchanged for its whole read — the same
    stat :class:`SourceIdentity` names an original by, so it compares with what the decision read."""
    return (file.file_path, staged.receipt.size_bytes, staged.receipt.source_mtime_ns)


async def _still_holds(
    db: AsyncSession,
    item: AutoQueueItem,
    move: Move,
    file: LibraryFile,
    staged: StagedSource,
    requirements: PrintRequirements,
    recheck: _Recheck,
) -> str | None:
    """Whether the move is still the one decided — after the target bytes were copied, before one write.

    Fresh facts, never the pass's verdicts:

    * the row read again — still movable, the same policy, and still what the
      decision read of it (:func:`_row_identity`);
    * the copy is the revision of the target file the decision read
      (:func:`_target_revision` — codex review r1, RB-R3: a plate re-sliced during
      the copy can keep its model, plate and filaments and still take 45 000
      seconds where the move was decided on 2000), and its requirements are the
      ones the decision read;
    * the plan engine's catalog of the line read again, in a session of its own
      (with the copy's family enrichment): the target plate's recipe (its yield
      and time — the move's ``k`` and finish) and the job's own yield (its moved
      parts) are still the numbers the move was computed from;
    * every receiver the estimate put prints on — its queue still accepting new
      work, a fresh busy set, its live feed, idleness, ``require_previous_success``,
      its model read from its row and the farm's «compatible models» setting read
      again (RB-R2: the router stops giving a compatible printer the job the
      moment that setting is off) — resolved again.

    The interlocks are NOT read again (C3-1): the pass's set stands, and no sensor
    is polled under a capture. ``None`` = holds; otherwise the reason it is
    skipped, with no cooldown (a refused move sets no ``rebalanced_at``).

    ⚠️ This is evidence, not ownership: it runs before the publication and the
    profile read. That the row is still the decided one when it is WRITTEN is
    :func:`_claim_row`'s, under the row's lock.
    """
    fresh = await db.get(AutoQueueItem, item.id, populate_existing=True)
    changed = _row_changed(fresh, item.id, recheck)
    if changed:
        return changed
    if _target_revision(file, staged) != recheck.oracle.plate_revision(move.plate.plate_id):
        return "eligibility_changed"
    project_id = recheck.projects.get(fresh.project_line_id)
    # ⚠️ A session of its own (codex review r2, RB2-R1). This one still holds every
    # file, plate, product and part the decision loaded — it never expires on
    # commit — and a new SELECT hands back those objects as they were then, so a
    # recipe another writer committed during the copy would be compared with
    # itself. A fresh session reads what is committed now; the catalog keeps only
    # plain values, so nothing is loaded through it after it closes. Never
    # ``expire_all`` here instead: the rows of this pass would then load lazily,
    # which an async session cannot do.
    async with _database.async_session() as current:
        requirements = await enrich_family_filament_types(current, requirements)
        catalog = await load_line_catalog(current, [project_id] if project_id is not None else [])
    if _content_key(requirements) != recheck.oracle.plate_content(move.plate.plate_id):
        return "eligibility_changed"
    option = next(
        (o for o in catalog.options_by_line.get(fresh.project_line_id, ()) if o.plate_id == move.plate.plate_id), None
    )
    if (
        option != move.plate
        or catalog.yield_of(fresh.project_line_id, fresh.library_file_id, fresh.plate_id) != move.moved_parts
    ):
        return "eligibility_changed"
    models = {
        printer_id: printer_manager.effective_model_for(printer_id, model)
        for printer_id, model in (
            await db.execute(
                select(Printer.id, Printer.model)
                .join(PrinterQueue, PrinterQueue.printer_id == Printer.id)
                .where(Printer.id.in_(move.receivers), *_accepts_new_work())
            )
        ).all()
    }
    allow_compatible = await _compatible_models_allowed(db)
    busy = await busy_printer_ids(db)
    policy = auto_policy(fresh)
    for printer_id in move.receivers:
        snapshot = printer_manager.get_feed_snapshot(printer_id)
        if (
            printer_id not in models
            or not snapshot.connected
            or printer_id in busy
            or printer_id in recheck.interlocked
            or not scheduler._is_printer_idle(printer_id, require_plate_clear=True)
        ):
            return "eligibility_changed"
        if fresh.require_previous_success and not await scheduler.previous_print_succeeded(db, printer_id):
            return "eligibility_changed"
        verdict = model_compatibility(requirements.model, models[printer_id])
        if verdict != "exact" and not (verdict == "compatible" and allow_compatible):
            return "eligibility_changed"
        result = resolve_filament_routing(
            requirements, policy, snapshot, exact_model=verdict == "exact", prefer_lowest=recheck.prefer_lowest
        )
        if result.status != "compatible":
            return "eligibility_changed"
    return None


async def _claim_row(db: AsyncSession, item: AutoQueueItem, recheck: _Recheck) -> str | None:
    """Read the row again UNDER ITS LOCK, after every long await, and compare it with the decision.

    Codex review r1, RB-R1: two runs — the tick's and a button's — may both decide
    to move one job, and both pass :func:`_still_holds`; the first converts it and
    adds k − 1 companions, and a second conversion from the row this session
    read before the publication would add k − 1 more and overwrite where the job
    came from. A lock taken AFTER a read does not make that read fresh, so the
    row is read here, inside the lock, and written only if it is still the row
    the decision read — the router's claim, a cancel (the row is deleted) and
    the other run's conversion each leave it something else.

    The order is the router's own (``_assign``): the order's row first
    (:func:`order_resources.policy_for`, taken by the caller), then the job's.
    On SQLite ``take_write_lock`` takes the one write lock before the read; on
    PostgreSQL ``FOR UPDATE`` waits for a writer that holds the row and then
    reads what it committed. Nothing long runs under it: the copy and the
    publication are already done.
    """
    await take_write_lock(db, AutoQueueItem.__table__, item.id)
    fresh = await db.scalar(
        select(AutoQueueItem)
        .where(AutoQueueItem.id == item.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return _row_changed(fresh, item.id, recheck)


async def _apply(
    db: AsyncSession,
    item: AutoQueueItem,
    move: Move,
    *,
    now: datetime,
    cache: PrintRequirementsCache,
    current_user: User | None,
    result: RebalanceResult,
    recheck: _Recheck,
) -> None:
    """One move, all-or-nothing: read and prepare EVERYTHING first, then write.

    ⚠️ **The decision is checked again between the copy and the first write**
    (:func:`_still_holds`): the copy released the transaction, and a feed, a
    queue, the target file, its recipe or the row itself may have moved
    meanwhile. A move whose evidence went is skipped as ``eligibility_changed``
    with no cooldown — the next pass reads the farm afresh. And the row is read
    once more under its lock right before it is written (:func:`_claim_row`): two
    runs that both passed the check must not both convert one job.

    Order matters three times over. The **bytes of the target file** are captured,
    the strict source read comes out of that copy, and the receiving model's
    print-option profile is fetched — all before the first assignment to ``item``,
    so a refusal or a DB error in any of them leaves the row exactly as it was.
    And the creation of the ``k − 1`` companions is undone by hand when it
    fails: the already-converted row rides along on the writer's own
    transaction, so without the undo a HALF move becomes durable — the row
    covering 2 of the 6 parts it used to claim, the companions never created,
    the line quietly four parts short. Restoring the twelve fields and reporting
    ``creation_failed`` is what keeps "a move" one thing — and when the writer
    got as far as its own ``commit``, undoing means DELETING the rows it made,
    not forgetting them.

    ⚠️ **A move changes the FILE, so it must change the bytes with it** (m173, the
    Task 6 review's C1). Since the spool, every reader — routing, eligibility, the
    promotion, both dispatch runners — resolves a row's source through its
    ``queue_source_id``. A conversion that rewrote ``library_file_id`` /
    ``plate_id`` / ``target_model`` and left the snapshot alone therefore printed
    the **source** model's plate on the **receiving** model's machine, with nothing
    in the row looking wrong: plate 1 of the old file resolves against plate 1 of
    the new one, so it does not even fail. Two fixes were rejected. *Clearing* the
    two columns drops the row back to reading its (new) original and gives up the
    guarantee the whole feature exists for — an external share that goes away takes
    the rebalanced job with it. *Capturing after* the mutation satisfies A10's first
    half and breaks its second: for as long as the copy runs the row is durably
    filed against a file it has no bytes for. So the capture comes first and a
    capture refusal is a ``refusal`` (``source_unreadable``), never a half-moved row.

    ⚠️ **The crash window, now that the order is the other way round.** The writer
    still commits first: ``add_items_to_auto_queue`` releases its transaction before
    it copies (spec §5 step 1 — a copy over a share may take minutes and must not be
    held open across). What used to be exposed was the conversion itself: the row
    was durable on the target file while the bytes were still being read, so a crash
    during the copy left a converted row with nothing behind it. That is closed — the
    copy is finished and published before the row moves. What remains is narrower
    and pre-dates the spool in kind: a crash inside the *companions'* own copy, after
    the conversion is committed, leaves the line ``(k − 1) × y`` parts short, and the
    recovery is a re-run of the rebalance, which covers it.

    ⚠️ **The companions capture the same file a second time.** The blob is deduped by
    ``sha256`` so nothing extra lands on disk, and the parse is shared through
    ``cache`` — the second read of the original is the price of
    ``add_items_to_auto_queue`` staying the single definition of "create auto rows",
    which it must be: a second construction path here is exactly the drift that
    writer was extracted to prevent.

    The conversion keeps every print option, the line, ``force_color_match`` and
    the position; the created rows take the saved profile for the receiving
    model — the operator's when a person pressed the button, the system row when
    the tick ran — the way the plan's own enqueue door does, with swap macros
    muted when the file bakes them. All ``k`` rows share one batch id. The
    profile decides the **toggles**; the source ROW decides the **job** — feed
    policy, AMS, power-off-after, previous-success — so an external-only print
    of six parts cannot become one external-only print and two AMS ones.

    ⚠️ The writer is handed the LINE and no ``project_id``: it derives the order
    from the line, so a row whose ``project_id`` points at an order that is gone
    (SQLite honours no FK action) cannot make the creation refuse.
    """

    # ``populate_existing``: the decision loaded this row into the session, which
    # never expires on commit — without it a plain SELECT hands back the row as it
    # was then, and the copy would go to where the file USED to be (codex review r2).
    file = (
        await db.execute(
            LibraryFile.active()
            .where(LibraryFile.id == move.plate.library_file_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if file is None:
        # Trashed or deleted between the plan and here. There is no source to
        # read, which is the same thing as a source nobody can read.
        result.skipped.append((item.id, "source_unreadable"))
        return
    staged = await _capture_target(db, item, move, file, result=result)
    if staged is None:
        return
    try:
        req = await staged_requirements(
            staged, cache, None, file, move.plate.plate_index, product_plate_id=move.plate.plate_id
        )
        changed = await _still_holds(db, item, move, file, staged, req, recheck)
        if changed is not None:
            await discard_staged(staged)
            logger.info(
                "Rebalance: item %s stays on %s — %s after the copy of plate %s",
                item.id,
                item.target_model,
                changed,
                move.plate.plate_id,
            )
            result.skipped.append((item.id, changed))
            return

        to_model = req.model or move.plate.model_label
        # The receiving model's saved profile — nothing of the row's, so it is read
        # before the row is claimed; what the row says is read after.
        profile_options: dict | None = None
        if move.k > 1:
            profile = await preference_options(db, current_user, to_model)
            profile_options = profile.for_auto_queue() if profile else {}
        blob = await publish_staged(staged, _no_rows_of_our_own)
    except HTTPException as exc:
        await discard_staged(staged)
        logger.info(
            "Rebalance: item %s stays on %s — plate %s of the copied file was refused: %s",
            item.id,
            item.target_model,
            move.plate.plate_id,
            exc.detail,
        )
        result.skipped.append((item.id, capture_skip_reason(exc)))
        return
    except BaseException:
        # A DB error, a cancellation at shutdown: give the staged bytes back rather
        # than leaving a ``.part`` for the grace window (§5 step 4).
        await discard_staged(staged)
        raise

    # Pinned across the handover: the blob is published and, until the commit
    # below, nobody owns it — a pin is what §9 offers for exactly that gap.
    async with queue_sources.pin(blob.id):
        from backend.app.services.order_resources import constrained, policy_for

        # Capturing a source releases the transaction. An operator may have
        # narrowed the order while that copy ran; never convert its queued file
        # using the earlier unrestricted snapshot.
        policy = await policy_for(db, item.project_id, project_line_id=item.project_line_id, lock=True)
        if constrained(policy):
            result.skipped.append((item.id, "order_resources"))
            return
        claimed = await _claim_row(db, item, recheck)
        if claimed is not None:
            # The blob stays published and unowned once the pin goes — the
            # collector's grace window, as after ``creation_failed``.
            await db.commit()
            logger.info(
                "Rebalance: item %s not moved to plate %s — %s when it was to be written",
                item.id,
                move.plate.plate_id,
                claimed,
            )
            result.skipped.append((item.id, claimed))
            return

        # From here to the commit the row is the one the decision read, and nobody
        # else can change it: everything taken from it is read now.
        from_model = item.target_model or move.from_model
        batch_id = item.batch_id or (str(uuid.uuid4()) if move.k > 1 else None)
        payload: dict | None = None
        if profile_options is not None:
            options = dict(profile_options)
            options.update(override_options(item.order_print_options, to_model))
            # The companions are router rows, whose swap fields are inert: their assignment decides per
            # printer from the captured declaration (swap-source-lifecycle spec r4 §8), not this library row.
            # After the profile, so the row wins: these four are not toggles the
            # profile has an opinion about (``SharedQueueOptions`` carries none of
            # them) — they say how THIS job feeds and what happens when it ends.
            # ``feed_policy`` may be the row's own ``"auto"``; the writer normalises
            # it exactly as it does for every other door.
            payload = {
                "auto_eject_enabled": item.auto_eject,
                "auto_eject_settings": item.auto_eject_settings or {},
                "auto_eject_skip_acknowledged": True,  # Preserve the already accepted job policy.
                **options,
                "use_ams": item.use_ams,
                "feed_policy": item.feed_policy,
                "auto_off_after": item.auto_off_after,
                "require_previous_success": item.require_previous_success,
                "force_color_match": item.force_color_match,
                # The job's routing policy is the one its fit was judged by (spec R4):
                # a companion that took the schema's default would route looser than
                # the row it is a copy of.
                "allow_base_material_match": item.allow_base_material_match,
            }
        logger.info(
            "Rebalance: line %s item %s %s → %s: %d print(s) of plate %s (yield %d, %d parts, surplus %d), "
            "finish %.0fs vs home %s",
            item.project_line_id,
            item.id,
            from_model,
            to_model,
            move.k,
            move.plate.plate_id,
            move.plate.yield_parts,
            move.moved_parts,
            move.surplus,
            move.finish,
            "none" if move.home_finish is None else f"{move.home_finish:.0f}s",
        )
        # The row as it is, so the creation step below can put it back. ⚠️ The two
        # snapshot columns are part of it: a row restored to the source model's file
        # while keeping the target file's bytes would be the same wrong-file print as
        # C1, arrived at from the other direction.
        before = {
            field_name: getattr(item, field_name)
            for field_name in (
                "archive_id",
                "library_file_id",
                "plate_id",
                "target_model",
                "required_filament_types",
                "print_time_seconds",
                "waiting_reason",
                "batch_id",
                "rebalanced_at",
                "rebalanced_from_model",
                "queue_source_id",
                "source_snapshot",
            )
        }
        item.archive_id = None
        item.library_file_id = file.id
        item.plate_id = req.resolved_plate_id
        item.target_model = to_model
        item.required_filament_types = json.dumps(list(dict.fromkeys(f["type"] for f in req.used_filaments)))
        item.print_time_seconds = req.print_time_seconds
        item.waiting_reason = None
        item.batch_id = batch_id
        item.rebalanced_at = now
        item.rebalanced_from_model = from_model
        item.queue_source_id = blob.id
        item.source_snapshot = queue_sources.snapshot_for(staged.receipt, blob)
        # Durable while the pin still holds, so the reference the collector reads
        # exists before the pin that stood in for it goes.
        await db.commit()
        result.converted += 1
        result.moved_parts += move.moved_parts

        if payload is None:
            return
        created: list[AutoQueueItem] = []
        try:
            created = await add_items_to_auto_queue(
                db,
                AutoQueueItemCreate(
                    library_file_id=file.id,
                    plate_id=req.resolved_plate_id,
                    quantity=move.k - 1,
                    project_line_id=item.project_line_id,
                    **payload,
                ),
                current_user,
                requirements_cache=cache,
                order_print_options=item.order_print_options,
            )
            for row in created:
                row.batch_id = batch_id
                row.rebalanced_at = now
                row.rebalanced_from_model = from_model
            await db.flush()
        except Exception as exc:
            # Restore FIRST, delete SECOND, commit ONCE. The writer commits its own
            # rows, so anything that fails after it (the stamping loop, its flush)
            # leaves them durable — unstamped, filed under the line, holding parts
            # nobody owes and with no ``rebalanced_at`` to make the cooldown notice.
            # Restoring before the delete means the autoflush ``db.delete`` may run
            # can only ever write the row's ORIGINAL values, and the one commit that
            # follows makes both halves of the undo durable together.
            #
            # ⚠️ **The commit is unconditional now**, where it used to happen only if
            # rows had been created: the conversion is committed above, beside the
            # reference to the new blob, so an undo that only assigned the old values
            # back in memory would leave the converted row on disk and the row in the
            # session disagreeing with it.
            #
            # ⚠️ The blob stays published and becomes unowned when the pin goes. That
            # is the collector's business and not a leak: it is one shared object,
            # keyed by content, and the grace window releases it if nothing claims it.
            for field_name, value in before.items():
                setattr(item, field_name, value)
            if created:
                for row in created:
                    await db.delete(row)
            await db.commit()
            result.converted -= 1
            result.moved_parts -= move.moved_parts
            logger.warning(
                "Rebalance: line %s item %s stays on %s — creating its %d companion print(s) failed "
                "(%d already-created row(s) deleted): %s",
                item.project_line_id,
                item.id,
                before["target_model"],
                move.k - 1,
                len(created),
                exc,
            )
            result.skipped.append((item.id, "creation_failed"))
            return
        result.created += len(created)


async def _no_rows_of_our_own(_session: AsyncSession, _source: QueueSource) -> None:
    """``publish``'s ``attach`` when the caller writes its rows afterwards.

    Every other producer builds its job rows inside the publication's transaction,
    and this one cannot: the ``k − 1`` companions go through
    ``add_items_to_auto_queue``, which captures and publishes for itself, and
    ``publish`` holds the one storage guard while ``attach`` runs — a nested
    publication would deadlock on it. So the blob is published on its own and the
    converted row becomes its owner immediately afterwards, under a
    :func:`queue_sources.pin` that holds it against the collector for the gap (§9).
    """
    return None


async def _capture_target(
    db: AsyncSession,
    item: AutoQueueItem,
    move: Move,
    file: LibraryFile,
    *,
    result: RebalanceResult,
) -> StagedSource | None:
    """Copy the target file's bytes — before one field of ``item`` has changed.

    ``None`` means the move is off and the reason is already recorded: the row is
    still exactly as the plan found it, which is what makes a refusal here a plain
    ``refusal`` rather than the first half of a move (A10).

    ⚠️ **The transaction is released before the copy** (spec §5 step 1). A copy over
    a share can take minutes and on SQLite the caller's write lock would be held
    across all of it — one unreachable NAS stalling every other query. The commit
    makes the tick's placement pass, and any earlier move of this run, durable here;
    that already happened on every ``k > 1`` move, because the companions' writer
    commits the caller's session for the same reason.
    """
    try:
        plan = plan_capture(library_file=file)
    except HTTPException as exc:
        logger.info("Rebalance: item %s stays on %s — %s", item.id, item.target_model, exc.detail)
        result.skipped.append((item.id, capture_skip_reason(exc)))
        return None
    await db.commit()
    try:
        return await capture_staged(plan)
    except HTTPException as exc:
        logger.info(
            "Rebalance: item %s stays on %s — the copy of plate %s's file was refused: %s",
            item.id,
            item.target_model,
            move.plate.plate_id,
            exc.detail,
        )
        result.skipped.append((item.id, capture_skip_reason(exc)))
        return None
