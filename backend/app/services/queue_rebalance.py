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
``_assign``. "Idle" is asked only to decide who may RECEIVE moved work: a
printer with an empty queue that still waits for a plate clear only looks
free, and moving work to it would move it nowhere — so the receiver test is
the busy set AND ``PrintScheduler._is_printer_idle``, both readiness signals
the router's ranking already uses.

The cheap verdicts are reached before the expensive load: a cooling line, and a
row whose own model has an idle machine, are both answered without reading
anything. What is left genuinely needs the plan engine's catalog — "an idle
model with no candidate plate for THESE lines" cannot be answered from the
queue rows alone, and there is no cheaper source for it than the plates
themselves.

⚠️ **Never moved:** assigned, scheduled for a time, staged (``manual_start``),
pinned (slot-bound ``filament_overrides``), aimed at a location, sourced from
an archive, or not filed under an order line — see :func:`refusal`. Those
either belong to a printer already, carry an operator's explicit decision, or
have nothing to move to.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.project_line import ProjectLine
from backend.app.models.queue_source import QueueSource
from backend.app.models.user import User
from backend.app.schemas.auto_queue import AutoQueueItemCreate
from backend.app.schemas.project import RebalanceOut, RebalanceSkipped
from backend.app.services import queue_sources
from backend.app.services.auto_queue_add import add_items_to_auto_queue
from backend.app.services.auto_queue_eligibility import busy_printer_ids
from backend.app.services.farm_forecast import FarmSnapshot, load_snapshot, model_key, rank_active_orders
from backend.app.services.filament_policy import auto_policy
from backend.app.services.filament_requirements import PrintRequirementsCache
from backend.app.services.order_metrics import attribute, batch_contexts, line_accepts_materials
from backend.app.services.plan_engine import (
    _pick_key,
    counted_parts_by_line,
    line_yield,
    plate_recipe_index,
    recipes_for_row,
)
from backend.app.services.print_option_defaults import preference_options
from backend.app.services.print_scheduler import scheduler
from backend.app.services.product_composition import PlateRecipe, estimate_seconds, recipes_for_products
from backend.app.services.queue_source_capture import (
    StagedSource,
    capture_staged,
    discard_staged,
    plan_capture,
    publish_staged,
    staged_requirements,
)

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
    "creation_failed",
    "home_model_idle",
    "no_faster_model",
    "cooldown",
)


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


@dataclass
class FarmView:
    """The farm as the decision sees it: who may receive, and when each model is next free.

    A model absent from ``free_at_by_model`` has no machine that accepts new
    work — an item whose home that is moves whenever anything can take it.
    """

    idle_by_model: dict[str, int]
    free_at_by_model: dict[str, float]  # seconds from now


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


@dataclass
class MovePlan:
    moves: list[Move] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)  # (item_id, reason)


# ---------- the decision ----------


def _best_plate(options, model: str, wanted: int) -> PlateOption | None:
    """The line's best plate of ``model`` for covering ``wanted`` parts — by the plan engine's own key.

    Most useful parts per hour first, then least waste, then the shorter print,
    then the lower plate id. A plate without an estimate is never chosen: its
    finish could not be compared with waiting at home.
    """
    candidates = [o for o in options if o.model == model and o.yield_parts > 0 and o.seconds is not None]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda o: _pick_key(min(o.yield_parts, wanted), max(0, o.yield_parts - wanted), o.seconds, o.plate_id),
    )


def plan_moves(
    items: list[MovableItem],
    options_by_line: dict[int, list[PlateOption]],
    farm: FarmView,
    *,
    cooling: frozenset[int] | set[int] = frozenset(),
) -> MovePlan:
    """The decision, item by item, in the order ``items`` arrive (spec §3.2).

    Pure. The caller sorts the items — order rank, then queue order — and hands
    in the farm as it is now; this walks them once, consuming capacity as it
    goes, so an urgent order takes idle printers first and one pass never
    floods a model. Per item: skip if its line is cooling or its own model has
    an idle printer (the normal pass is about to place it); otherwise find,
    for every other model with capacity, the best plate, ``k = ceil(y_m / y_X)``
    prints of it and their finish ``ceil(k / c_X) × t_X``; move to the earliest
    finish that is not later than ``free_at(home) + t_m``.

    ``t_m`` unknown compares as 0 (home looks instant, so the item moves only
    when the other model finishes before home is even free). When a receiver's
    capacity reaches zero its ``free_at`` becomes the shortest chain a machine
    of it now holds, ``(k // c_X) × t_X``, so a later item whose HOME is that
    model sees the farm as it will be.
    """
    idle = dict(farm.idle_by_model)
    free_at = dict(farm.free_at_by_model)
    plan = MovePlan()
    for item in items:
        if item.line_id in cooling:
            plan.skipped.append((item.item_id, "cooldown"))
            continue
        if idle.get(item.home_model, 0) > 0:
            plan.skipped.append((item.item_id, "home_model_idle"))
            continue
        home_free = free_at.get(item.home_model)
        home_finish = None if home_free is None else home_free + (item.seconds or 0)
        best: tuple[tuple[float, int, str], str, PlateOption, int, float, int] | None = None
        for model, capacity in idle.items():
            if capacity <= 0 or model == item.home_model:
                continue
            plate = _best_plate(options_by_line.get(item.line_id, ()), model, item.yield_parts)
            if plate is None:
                continue
            k = -(-item.yield_parts // plate.yield_parts)
            finish = float(-(-k // capacity) * (plate.seconds or 0))
            if home_finish is not None and finish > home_finish:
                continue
            surplus = k * plate.yield_parts - item.yield_parts
            key = (finish, surplus, model)
            if best is None or key < best[0]:
                best = (key, model, plate, k, finish, surplus)
        if best is None:
            plan.skipped.append((item.item_id, "no_faster_model"))
            continue
        _key, model, plate, k, finish, surplus = best
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
            )
        )
        capacity = idle[model]
        idle[model] = max(0, capacity - k)
        if idle[model] == 0:
            free_at[model] = max(free_at.get(model, 0.0), float((k // capacity) * (plate.seconds or 0)))
    return plan


def home_wait_by_model(snapshot: FarmSnapshot) -> dict[str, float]:
    """``model_key → seconds until the earliest machine of that model that accepts work is free``.

    The same estimate the ETA uses (``farm_forecast.load_snapshot``): the head
    of the queue is the running print's remaining seconds, then the pending rows
    in position order; a row without an estimate counts nothing. A parked
    machine (``accepts_new_work=False``) never sets the figure — what it holds is
    not somewhere new work could go.
    """
    out: dict[str, float] = {}
    for machine in snapshot.printers:
        if not machine.accepts_new_work:
            continue
        key = model_key(machine.model)
        if key is None:
            continue
        busy_for = max(0.0, float(machine.running_seconds)) + float(
            sum(row.seconds for row in machine.queued if row.seconds and row.seconds > 0)
        )
        out[key] = min(out[key], busy_for) if key in out else busy_for
    return out


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


async def idle_printers_by_model(db: AsyncSession, busy: set[int]) -> dict[str, int]:
    """``model_key → printers that may RECEIVE moved work right now``.

    The same population ``printers_for_item`` routes over (active, not archived,
    auto-distribute eligible, queue not paused), minus the busy set, keeping only
    machines that are connected and idle by ``_is_printer_idle`` with the
    plate-clear gate honoured. ⚠️ Both halves, deliberately: an empty queue on a
    printer still waiting for its plate clear only looks free.

    ⚠️ ``status in ("paused", "error")`` is what ``print_scheduler.check_queue``
    refuses to dispatch from, mirrored here so a receiver is a machine that will
    actually PRINT what it is given: work moved onto such a queue would sit
    there while every later tick answered ``home_model_idle``.
    """
    rows = (
        await db.execute(
            select(Printer.id, Printer.model)
            .join(PrinterQueue, PrinterQueue.printer_id == Printer.id)
            .where(
                Printer.is_active.is_(True),
                Printer.archived.is_(False),
                PrinterQueue.auto_distribute_eligible.is_(True),
                PrinterQueue.is_paused.is_(False),
                PrinterQueue.status.not_in(("paused", "error")),
            )
        )
    ).all()
    # By printer id, so a printer that somehow answered the query twice (a
    # second queue row of its own) is still one machine of capacity.
    by_model: dict[str, set[int]] = {}
    for printer_id, model in rows:
        key = model_key(model)
        if key is None or printer_id in busy or not scheduler._is_printer_idle(printer_id, require_plate_clear=True):
            continue
        by_model.setdefault(key, set()).add(printer_id)
    return {key: len(ids) for key, ids in by_model.items()}


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
            AutoQueueItem.status == "pending",
            AutoQueueItem.assigned_to_item_id.is_(None),
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
    now: datetime | None = None,
) -> RebalanceResult:
    """Run the procedure (spec §3) over the whole router, one line, or named items.

    ``force`` ignores the per-line cooldown — both manual routes pass it; the
    tick does not. ``busy_printers`` lets the tick hand in the set it just
    updated; anybody else gets it freshly from ``busy_printer_ids``. Every
    conversion and creation is logged at INFO with the line, the models, ``k``
    and the two finish times compared.
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
    for row in rows:
        why = refusal(row)
        if why:
            result.skipped.append((row.id, why))
        else:
            movable_rows.append(row)
    if not movable_rows:
        return result

    busy = busy_printers if busy_printers is not None else await busy_printer_ids(db)
    idle = await idle_printers_by_model(db, busy)
    if not idle:
        # ``no_faster_model`` here means no printer of any model is free — the
        # closed list has no separate code, and to the operator the answer is
        # the same.
        result.skipped.extend((row.id, "no_faster_model") for row in movable_rows)
        return result

    # The cheap verdicts BEFORE the catalog: everything below this loads every
    # archive of every affected order, and a tick where every movable row is
    # cooling or has a machine of its own model standing free must not pay for
    # it. ``plan_moves`` keeps the same two checks — pure, cheap and the
    # decision's own — so it stays correct whoever calls it.
    cooling = set() if force else await cooling_lines(db, {row.project_line_id for row in movable_rows}, now)
    homes: dict[int, str] = {}
    candidates: list[AutoQueueItem] = []
    for row in movable_rows:
        if row.project_line_id in cooling:
            result.skipped.append((row.id, "cooldown"))
            continue
        home = model_key(row.target_model)
        if home is None:
            result.skipped.append((row.id, "no_yield"))
            continue
        if idle.get(home, 0) > 0:
            result.skipped.append((row.id, "home_model_idle"))
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
    rank = {project_id: i for i, project_id in enumerate(await rank_active_orders(db))}
    free_at = home_wait_by_model(await load_snapshot(db, now))

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
    plan = plan_moves(
        [item for _key, item in keyed],
        catalog.options_by_line,
        FarmView(idle_by_model=idle, free_at_by_model=free_at),
        cooling=cooling,
    )
    result.skipped.extend(plan.skipped)

    by_id = {row.id: row for row in candidates}
    cache = PrintRequirementsCache()
    for move in plan.moves:
        await _apply(db, by_id[move.item_id], move, now=now, cache=cache, current_user=current_user, result=result)
    return result


async def _apply(
    db: AsyncSession,
    item: AutoQueueItem,
    move: Move,
    *,
    now: datetime,
    cache: PrintRequirementsCache,
    current_user: User | None,
    result: RebalanceResult,
) -> None:
    """One move, all-or-nothing: read and prepare EVERYTHING first, then write.

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

        from_model = item.target_model or move.from_model
        to_model = req.model or move.plate.model_label
        batch_id = item.batch_id or (str(uuid.uuid4()) if move.k > 1 else None)
        # Everything the creation needs, read while the row is still untouched.
        payload: dict | None = None
        if move.k > 1:
            profile = await preference_options(db, current_user, to_model)
            options = profile.for_auto_queue() if profile else {}
            if file.swap_compatible:
                options["execute_swap_macros"] = False
                options["swap_macro_events"] = None
            # After the profile, so the row wins: these four are not toggles the
            # profile has an opinion about (``SharedQueueOptions`` carries none of
            # them) — they say how THIS job feeds and what happens when it ends.
            # ``feed_policy`` may be the row's own ``"auto"``; the writer normalises
            # it exactly as it does for every other door.
            payload = {
                **options,
                "use_ams": item.use_ams,
                "feed_policy": item.feed_policy,
                "auto_off_after": item.auto_off_after,
                "require_previous_success": item.require_previous_success,
                "force_color_match": item.force_color_match,
            }
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
        result.skipped.append((item.id, "source_unreadable"))
        return
    except BaseException:
        # A DB error, a cancellation at shutdown: give the staged bytes back rather
        # than leaving a ``.part`` for the grace window (§5 step 4).
        await discard_staged(staged)
        raise

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
    # Pinned across the handover: the blob is published and, until the commit
    # below, nobody owns it — a pin is what §9 offers for exactly that gap.
    async with queue_sources.pin(blob.id):
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
        result.skipped.append((item.id, "source_unreadable"))
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
        result.skipped.append((item.id, "source_unreadable"))
        return None
