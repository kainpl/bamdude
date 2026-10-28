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

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, Protocol

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
from backend.app.services.auto_queue_eligibility import busy_printer_ids
from backend.app.services.farm_forecast import FarmSnapshot, load_snapshot, model_key, rank_active_orders
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
from backend.app.utils.model_compatibility import model_compatibility

if TYPE_CHECKING:
    from backend.app.services.printer_feed_snapshot import PrinterFeedSnapshot

logger = logging.getLogger(__name__)

REBALANCE_COOLDOWN_SECONDS = 300
REBALANCE_SETTING_KEY = "auto_queue_rebalance_models"

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
    item_id: int
    line_id: int
    home_model: str  # ``model_key`` of the item's target_model
    yield_parts: int  # y_m — what this item covers for its line
    seconds: int | None  # t_m


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
    """One online printer that organisationally accepts new auto-queue work.

    ``ready`` is a fact of the pass, never derived from the ledger: a printer
    awaiting its plate clear, held by a Home Assistant interlock or in the busy
    set may well be forecast at 0 s (spec A19), and still receives nothing.
    ``free_after`` is its ledger seed — 0 when ready, else the forecast.
    """

    printer_id: int
    model: str  # ``model_key`` of the printer's effective model
    ready: bool
    free_after: float


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


def _home_verdict(
    item: MovableItem, machines: list[Machine], farm: FarmView, ledger: dict[int, float]
) -> tuple[str | None, float | None]:
    """Whether the job stays home, and when its earliest fit home printer is free.

    ``home_model_idle`` when a home printer is ready, free in the ledger and can
    print the job; ``eligibility_unknown`` when none can but one such printer's
    answer is unknown (spec R8 — the router may place it there as soon as the
    feed is read). Otherwise ``None`` and the earliest ledger time among the
    homes that CAN print it, busy or not ready included (Q2: the current feed of
    a busy printer, with its forecast) — ``None`` when no home can. A home that
    cannot print it, or whose answer is unknown while it is busy or not ready,
    dates nothing.
    """
    home_free: float | None = None
    unknown_ready = False
    for machine in machines:
        if _model_verdict(item.home_model, machine.model, farm) is None:
            continue
        fit = farm.fits.home(item.item_id, machine.printer_id)
        free = ledger[machine.printer_id]
        free_now = machine.ready and free == 0
        if fit.verdict == "yes":
            if free_now:
                return "home_model_idle", None
            home_free = free if home_free is None else min(home_free, free)
        elif fit.verdict == "unknown" and free_now:
            unknown_ready = True
    return ("eligibility_unknown" if unknown_ready else None), home_free


def _receivers(
    item: MovableItem,
    plate: PlateOption,
    machines: list[Machine],
    farm: FarmView,
    ledger: dict[int, float],
    unknown: set[str | None],
) -> list[int]:
    """The ready, free printers that can print ``plate`` for this job — in the router's order.

    The router ranks ``(ready, exact, color_matches, -printer_id)``; every one
    here is ready, so exact model first, then colour matches, then the lower id.
    An unknown answer gives no capacity (spec R8) and is remembered for the reason.
    """
    ranked = []
    for machine in machines:
        # The free-and-ready test first: once the free printers are taken, a pass
        # walks hundreds of jobs that ask nothing more of any printer.
        if not machine.ready or ledger[machine.printer_id] != 0:
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
    return [-key[2] for key in ranked]


def _no_target_reason(unknown: set[str | None]) -> str:
    if not unknown:
        return "no_eligible_target"
    if unknown == {TARGET_SOURCE_UNREADABLE}:
        return "source_unreadable"
    return "eligibility_unknown"


def plan_moves(
    items: list[MovableItem],
    options_by_line: dict[int, list[PlateOption]],
    farm: FarmView,
    *,
    cooling: frozenset[int] | set[int] = frozenset(),
) -> MovePlan:
    """The decision, item by item, in the order ``items`` arrive.

    Pure, given ``farm.fits``. The caller sorts the items — order rank, then
    queue order — and this walks them once over ONE ledger, printer id →
    seconds until free (spec R12), so an urgent order takes the free printers
    first and no printer is capacity twice, across plates, lines or compatible
    models.

    Per item: skip if its line is cooling, or a home printer would take it now
    (:func:`_home_verdict`). Otherwise every alternative plate of another model
    gets its OWN receivers — ready, free in the ledger, and able to print it
    (:func:`_receivers`); per model the plan engine's key picks the best plate
    that has any, ``k = ceil(y_m / y_X)`` prints of it on ``c = min(k, n)`` of
    its receivers finish at ``ceil(k / c) × t_X``, and the item moves to the
    earliest finish not later than ``home_free + t_m`` — or to any fit target
    when no home can print it at all. ``t_m`` unknown compares as 0.

    A move writes its rounds into the ledger: ``q, r = divmod(k, c)``, the first
    ``r`` receivers ``(q + 1) × t``, the rest ``q × t``. A used printer is no
    longer free, and its time is what a later item whose home it is compares
    against. Alternatives that were not chosen change nothing.
    """
    ledger = {pid: (0.0 if m.ready else float(m.free_after)) for pid, m in farm.machines.items()}
    machines = sorted(farm.machines.values(), key=lambda m: m.printer_id)
    plan = MovePlan()
    for item in items:
        if item.line_id in cooling:
            plan.skipped.append((item.item_id, "cooldown"))
            continue
        stay, home_free = _home_verdict(item, machines, farm, ledger)
        if stay is not None:
            plan.skipped.append((item.item_id, stay))
            continue
        home_finish = None if home_free is None else home_free + (item.seconds or 0)
        unknown: set[str | None] = set()
        by_model: dict[str, list[tuple[PlateOption, list[int]]]] = {}
        for plate in options_by_line.get(item.line_id, ()):
            # A plate without an estimate is never a target: its finish could not be compared.
            if plate.model == item.home_model or plate.yield_parts <= 0 or plate.seconds is None:
                continue
            receivers = _receivers(item, plate, machines, farm, ledger, unknown)
            if receivers:
                by_model.setdefault(plate.model, []).append((plate, receivers))
        best: tuple[tuple[float, int, str], str, PlateOption, list[int], int, float, int] | None = None
        for model, candidates in by_model.items():
            # The plan engine's own key, among the plates that have a fit receiver:
            # most useful parts per hour, least waste, the shorter print, the lower id.
            plate, receivers = min(
                candidates,
                key=lambda pair: _pick_key(
                    min(pair[0].yield_parts, item.yield_parts),
                    max(0, pair[0].yield_parts - item.yield_parts),
                    pair[0].seconds,
                    pair[0].plate_id,
                ),
            )
            k = -(-item.yield_parts // plate.yield_parts)
            chosen = receivers[: min(k, len(receivers))]
            finish = float(-(-k // len(chosen)) * plate.seconds)
            if home_finish is not None and finish > home_finish:
                continue
            surplus = k * plate.yield_parts - item.yield_parts
            key = (finish, surplus, model)
            if best is None or key < best[0]:
                best = (key, model, plate, chosen, k, finish, surplus)
        if best is None:
            plan.skipped.append((item.item_id, "no_faster_model" if by_model else _no_target_reason(unknown)))
            continue
        _key, model, plate, chosen, k, finish, surplus = best
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
                receivers=tuple(chosen),
            )
        )
        rounds, extra = divmod(k, len(chosen))
        for index, printer_id in enumerate(chosen):
            ledger[printer_id] = float((rounds + (1 if index < extra else 0)) * plate.seconds)
    return plan


def free_after_by_printer(snapshot: FarmSnapshot) -> dict[int, float]:
    """``printer_id → seconds until it is free``, the ledger's seed for a printer that is not ready.

    The same estimate the ETA uses (``farm_forecast.load_snapshot``): the head
    of the queue is the running print's remaining seconds, then the pending rows
    in position order; a row without an estimate counts nothing.
    """
    return {
        machine.printer_id: max(0.0, float(machine.running_seconds))
        + float(sum(row.seconds for row in machine.queued if row.seconds and row.seconds > 0))
        for machine in snapshot.printers
    }


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


def _accepts_new_work() -> tuple:
    """A printer that may take new auto-queue work: the pass's population, and the final check's too."""
    return (
        Printer.is_active.is_(True),
        Printer.archived.is_(False),
        PrinterQueue.auto_distribute_eligible.is_(True),
        PrinterQueue.is_paused.is_(False),
        PrinterQueue.status.not_in(("paused", "error")),
    )


@dataclass(frozen=True)
class PassPrinter:
    """One printer as a rebalancing pass sees it — its facts read ONCE for the whole pass (spec R14)."""

    printer_id: int
    model_name: str  # the effective model, as the resolver and ``model_compatibility`` read it
    key: str  # its ``model_key``
    snapshot: PrinterFeedSnapshot
    ready: bool


async def load_printers(db: AsyncSession, busy: set[int], interlocked: set[int]) -> dict[int, PassPrinter]:
    """The online printers that may take new auto-queue work, with their feed snapshot and readiness.

    The same population ``printers_for_item`` routes over (active, not archived,
    auto-distribute eligible, queue not paused) — narrowed to a queue that
    ``print_scheduler.check_queue`` would dispatch from: ⚠️ ``status in
    ("paused", "error")`` is what it refuses, so work moved onto such a queue
    would sit there. An offline printer is left out whole (spec R7): it neither
    vetoes, nor dates a home, nor receives — not waiting for it to come back.

    ``ready`` = outside the busy set, idle by ``_is_printer_idle`` with the
    plate-clear gate honoured, and not held by a Home Assistant interlock of
    THIS pass's set (C3-1): ⚠️ an empty queue on a printer still waiting for its
    plate clear, or behind an alerting sensor, only looks free.
    """
    rows = (
        await db.execute(
            select(Printer.id, Printer.model)
            .join(PrinterQueue, PrinterQueue.printer_id == Printer.id)
            .where(*_accepts_new_work())
        )
    ).all()
    printers: dict[int, PassPrinter] = {}
    # By printer id, so a printer that somehow answered the query twice (a
    # second queue row of its own) is still one machine.
    for printer_id, model in rows:
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
        printers[printer_id] = PassPrinter(printer_id, name, key, snapshot, ready)
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

    def add_job(self, item: AutoQueueItem, requirements: PrintRequirements, policy: RoutingPolicy) -> None:
        key = self._req_keys.get(id(requirements))
        if key is None:
            key = self._req_keys[id(requirements)] = (requirements.source_identity, _content_key(requirements))
        self._jobs[item.id] = _Job(requirements, key, policy, policy.fingerprint, bool(item.require_previous_success))

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
    from backend.app.models.project import Project
    from backend.app.services.order_resources import constrained

    resources = dict(
        (
            await db.execute(
                select(Project.id, Project.printer_resources).where(Project.printer_resources.is_not(None))
            )
        ).all()
    )
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

    busy = busy_printers if busy_printers is not None else await busy_printer_ids(db)
    if interlocked_printers is None:
        # A manual door reads the interlocks once; the tick hands in the set its
        # router pass read (an empty set included). A failed read is the
        # caller's error, never «nothing is interlocked».
        from backend.app.services.ha_sensor_manager import ha_sensor_manager

        interlocked_printers = set(await ha_sensor_manager.blocked_printers(db))
    printers = await load_printers(db, busy, set(interlocked_printers))
    if not any(printer.ready for printer in printers.values()):
        # No printer is free: nothing can receive, and nothing can hold a job at
        # home either — answered without reading a single file.
        result.skipped.extend((row.id, "no_eligible_target") for row in movable_rows)
        return result

    # The cheap verdicts BEFORE the catalog: everything below this loads every
    # archive of every affected order, and a tick where every movable row is
    # cooling or has a free home printer that can print it must not pay for it.
    # ``plan_moves`` keeps the same checks — pure and the decision's own — so it
    # stays correct whoever calls it.
    cooling = set() if force else await cooling_lines(db, {row.project_line_id for row in movable_rows}, now)
    allow_compatible = await _compatible_models_allowed(db)
    prefer_lowest = await scheduler._get_bool_setting(db, "prefer_lowest_filament", default=True)
    cache = requirements_cache if requirements_cache is not None else PrintRequirementsCache()
    jobs: list[tuple[AutoQueueItem, str, PrintRequirements]] = []
    # One read per distinct source for the whole pass (spec R14): 358 copies of one
    # job are one stat, one parse and one family lookup, not 358 of each.
    read: dict[tuple, PrintRequirements] = {}
    for row in movable_rows:
        if row.project_line_id in cooling:
            result.skipped.append((row.id, "cooldown"))
            continue
        home = model_key(row.target_model)
        if home is None:
            result.skipped.append((row.id, "no_yield"))
            continue
        # The job's ACCEPTED source — its captured bytes, never the library's
        # current copy (spec R4). Unreadable: this job alone stays (R7).
        source = _source_key(row)
        requirements = read.get(source)
        if requirements is None:
            requirements = read[source] = await read_item_requirements(db, row, cache)
        if requirements.status != "ok":
            result.skipped.append((row.id, _source_skip(requirements.reason)))
            continue
        jobs.append((row, home, requirements))
    if not jobs:
        return result
    previous_ok: dict[int, bool] = {}
    if any(row.require_previous_success for row, _home, _req in jobs):
        # A printer's fact, read once; whether it applies is each job's own flag.
        previous_ok = {pid: await scheduler.previous_print_succeeded(db, pid) for pid in sorted(printers)}
    oracle = _FitOracle(printers, prefer_lowest=prefer_lowest, previous_ok=previous_ok)
    unseeded = FarmView(
        machines={pid: Machine(pid, p.key, p.ready, 0.0) for pid, p in printers.items()},
        fits=oracle,
        allow_compatible=allow_compatible,
    )
    machines_by_id = sorted(unseeded.machines.values(), key=lambda m: m.printer_id)
    zero_ledger = dict.fromkeys(unseeded.machines, 0.0)
    homes: dict[int, str] = {}
    candidates: list[AutoQueueItem] = []
    evaluated: dict[int, tuple] = {}
    for row, home, requirements in jobs:
        oracle.add_job(row, requirements, auto_policy(row))
        evaluated[row.id] = _row_identity(row)
        probe = MovableItem(item_id=row.id, line_id=row.project_line_id, home_model=home, yield_parts=0, seconds=None)
        stay, _home_free = _home_verdict(probe, machines_by_id, unseeded, zero_ledger)
        if stay is not None:
            result.skipped.append((row.id, stay))
            continue
        homes[row.id] = home
        candidates.append(row)
    if not candidates:
        return result

    line_projects: dict[int, int] = dict(
        (
            await db.execute(
                select(ProjectLine.id, ProjectLine.project_id).where(
                    ProjectLine.id.in_({row.project_line_id for row in candidates})
                )
            )
        ).all()
    )
    catalog = await load_line_catalog(db, sorted(set(line_projects.values())))
    await _read_alternative_plates(db, catalog, candidates, homes, cache, oracle)
    rank = {project_id: i for i, project_id in enumerate(await rank_active_orders(db))}
    seeds = free_after_by_printer(await load_snapshot(db, now))
    farm = FarmView(
        machines={
            pid: Machine(pid, p.key, p.ready, 0.0 if p.ready else seeds.get(pid, 0.0)) for pid, p in printers.items()
        },
        fits=oracle,
        allow_compatible=allow_compatible,
    )

    keyed: list[tuple[tuple[int, int, int], MovableItem]] = []
    for row in candidates:
        parts = catalog.yield_of(row.project_line_id, row.library_file_id, row.plate_id)
        if parts <= 0:
            result.skipped.append((row.id, "no_yield"))
            continue
        order_rank = rank.get(line_projects.get(row.project_line_id), len(rank))
        keyed.append(
            (
                (order_rank, row.position, row.id),
                MovableItem(
                    item_id=row.id,
                    line_id=row.project_line_id,
                    home_model=homes[row.id],
                    yield_parts=parts,
                    seconds=row.print_time_seconds,
                ),
            )
        )
    keyed.sort(key=lambda pair: pair[0])
    # ``cooling`` is handed in although no cooling row reached here: the pure
    # function's own guarantee should not depend on a caller's pre-filter.
    plan = plan_moves([item for _key, item in keyed], catalog.options_by_line, farm, cooling=cooling)
    result.skipped.extend(plan.skipped)
    logger.debug(
        "Rebalance: %d candidate(s), %d move(s); fit resolves %d, cache hits %d",
        len(keyed),
        len(plan.moves),
        oracle.resolves,
        oracle.hits,
    )

    by_id = {row.id: row for row in candidates}
    recheck = _Recheck(
        interlocked=set(interlocked_printers),
        prefer_lowest=prefer_lowest,
        oracle=oracle,
        rows=evaluated,
        projects=line_projects,
    )
    for move in plan.moves:
        await _apply(
            db,
            by_id[move.item_id],
            move,
            now=now,
            cache=cache,
            current_user=current_user,
            result=result,
            recheck=recheck,
        )
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
    * the plan engine's catalog of the line read again: the target plate's recipe
      (its yield and time — the move's ``k`` and finish) and the job's own yield
      (its moved parts) are still the numbers the move was computed from;
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
    if _content_key(requirements) != recheck.oracle.plate_content(move.plate.plate_id):
        return "eligibility_changed"
    project_id = recheck.projects.get(fresh.project_line_id)
    catalog = await load_line_catalog(db, [project_id] if project_id is not None else [])
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

    file = (
        await db.execute(LibraryFile.active().where(LibraryFile.id == move.plate.library_file_id))
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
        changed = await _still_holds(db, item, move, file, staged, await enrich_family_filament_types(db, req), recheck)
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
