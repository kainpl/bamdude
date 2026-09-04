"""What to print next for each line of an order — greedy covering (spec pass 3).

**Two greedy passes live in this codebase and they are not the same algorithm.**
``order_metrics.attribute`` deals finished archive part rows out across the lines
that count them: it distributes FACT, one part row at a time, and the question it
answers is "whose print was that". ``cover`` here picks plates to print for ONE
line until its outstanding parts are covered: it plans WORK, one whole plate at a
time, and the question it answers is "what next". They share the word "greedy"
and nothing else — keep the names ``attribute`` and ``cover`` apart in code,
tests and the vault, or a change to one reads as a change to both.

The engine proper (:func:`plan_lines` and everything above it) is pure: no
session, no printer state, no clock. Only :func:`plan_for_order` and
:func:`queued_yield_by_line` touch the database. Routing is not dispatching —
nothing here asks whether a printer is ready, and nothing here may start to.

**Cost.** The app already has one filament price and it is farm-wide:
``services.filament_cost.default_rate_per_kg`` reads the ``default_filament_cost``
setting, a price per KILOGRAM, and is the same source the archive cost estimate
uses (``archive.py`` → ``cost_of(grams, await default_rate_per_kg(db))``).
:func:`plan_for_order` divides it by 1000 for the per-gram price the rows want.
An unset rate is ``0.0`` there and becomes ``price_per_gram=None`` here, so
``cost`` stays ``None`` end to end — never ``0.0``, which would claim the print
was free. No new setting is added by this pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.product import ProductPlate
from backend.app.models.project_line import ProjectLine
from backend.app.services.filament_cost import default_rate_per_kg
from backend.app.services.order_metrics import LineFigures, OrderContext, attribute, load_order_context
from backend.app.services.product_composition import PlateRecipe, estimate_seconds, recipes_for_products

# A defence, not a feature: a plate whose yield somehow never shrinks the
# outstanding map would otherwise spin forever inside a request.
MAX_ITERATIONS = 10_000

Candidate = tuple[ProductPlate, LibraryFile, PlateRecipe]


@dataclass
class PlanRow:
    """One plate, printed ``count`` times.

    ``print_time_seconds`` / ``filament_used_grams`` / ``cost`` are PER PRINT —
    the count is the multiplier, so the frontend can re-do the arithmetic while
    the operator edits the count without asking the server again.
    """

    plate_id: int  # ProductPlate.id
    library_file_id: int
    plate_index: int  # 0 = the whole file
    filename: str
    count: int = 0
    useful: dict[int, int] = field(default_factory=dict)  # part_id → outstanding covered, summed over the prints
    print_time_seconds: int | None = None
    filament_used_grams: float | None = None
    cost: float | None = None
    # Sliced but timeless: the plate ranked on its useful count alone (time 1),
    # so the operator can see why it out-ranked a plate with a real estimate.
    time_unknown: bool = False


@dataclass
class LinePlan:
    """``outstanding_before`` and ``surplus_after`` list only non-zero entries —
    an empty ``outstanding_before`` is exactly "this line needs nothing"."""

    line_id: int
    product_id: int
    material: str | None
    outstanding_before: dict[int, int] = field(default_factory=dict)
    rows: list[PlanRow] = field(default_factory=list)
    surplus_after: dict[int, int] = field(default_factory=dict)
    unsatisfiable: list[int] = field(default_factory=list)
    candidates: list[int] = field(default_factory=list)  # ProductPlate ids eligible for this line
    not_sliced: list[int] = field(default_factory=list)


@dataclass
class PlanTotals:
    prints: int = 0
    print_time_seconds: int | None = None  # None as soon as ONE row has no estimate
    filament_used_grams: float = 0.0
    cost: float | None = None  # None with no farm rate, and also when no row could be costed


@dataclass
class OrderPlan:
    """The rows stay bare ids; the two name maps ride BESIDE them.

    Every id the plan can mention is already named in the ``OrderContext`` the
    plan was built from, so carrying the maps out costs nothing and spares the
    route two SELECTs to look up rows it just had in memory. They are lookup
    tables, not part of a row's identity — nothing downstream may key off a
    name, and a missing entry is a "?" placeholder, never an error.
    """

    lines: list[LinePlan] = field(default_factory=list)
    totals: PlanTotals = field(default_factory=PlanTotals)
    part_names: dict[int, str] = field(default_factory=dict)  # ProductPart.id → name
    product_names: dict[int, str] = field(default_factory=dict)  # Product.id → name
    # ``MAX_ITERATIONS`` stopped the covering of at least one line, so the rows
    # below are a PREFIX of the plan and the totals are a prefix's totals. It
    # rides on the plan because a silent guard reads as a finished answer — see
    # :func:`cover`.
    truncated: bool = False


def line_yield(recipe: PlateRecipe, counted_part_ids: set[int]) -> dict[int, int]:
    """The plate's yield toward THIS line: only parts the line's product counts.

    A plate is shared (two products' lids on one bed) and a product may zero a
    part it does not want counted (``qty_per_unit = 0``). Neither of those is
    this line's output, so neither enters its useful count, its waste or its
    surplus — the pass-1 shared-plate rule, applied to planning.
    """
    return {pid: n for pid, n in recipe.yield_by_part.items() if pid in counted_part_ids and n > 0}


def _material_ok(material: str | None, materials: set[str]) -> bool:
    """Mirror of ``order_metrics._line_accepts``: a line with no material takes
    every plate, and a line with one takes only plates that carry it. A plate
    whose materials are unknown (empty set) therefore matches no constrained
    line — the same reading attribution gives an archive with no filament type.
    """
    return material is None or material.strip().upper() in materials


def _pick_key(useful: int, waste: int, secs: int | None, plate_id: int) -> tuple:
    """Spec decision 4, as a sort key (lower is better).

    Highest ``useful / print_time_seconds`` first — a sliced plate with no
    estimate divides by 1, i.e. it competes on its useful count alone. Ties go
    to lower waste, then to the shorter print (an unknown time sorts last, so a
    timeless plate never wins a tie it did not earn on score), then to the lower
    plate id so the same order comes back on every read.

    ``secs`` arrives already normalised by :func:`estimate_seconds`, so ``None`` is the
    only spelling of "no estimate" this key ever sees.
    """
    return (-(useful / (secs or 1)), waste, (secs is None, secs or 0), plate_id)


def _row_for(plate: ProductPlate, file: LibraryFile, recipe: PlateRecipe, price_per_gram: float | None) -> PlanRow:
    grams = recipe.filament_used_grams
    secs = estimate_seconds(recipe)
    return PlanRow(
        plate_id=plate.id,
        library_file_id=plate.library_file_id,
        plate_index=plate.plate_index,
        filename=file.filename,
        print_time_seconds=secs,
        filament_used_grams=grams,
        cost=round(grams * price_per_gram, 2) if grams and price_per_gram else None,
        time_unknown=secs is None,
    )


def cover(
    outstanding: dict[int, int],
    candidates: list[Candidate],
    counted: set[int],
    price_per_gram: float | None,
) -> tuple[list[PlanRow], dict[int, int], bool]:
    """Greedy covering (spec decision 4): plates for one line, until nothing helps.

    Returns the rows in pick order — one row per distinct plate, its ``count``
    and its ``useful`` aggregated over the prints — the surplus each counted
    part ends the plan with, and whether the ``MAX_ITERATIONS`` guard stopped
    it. ``outstanding`` may carry zeros (a part already covered): they never
    attract a pick but they do bound the surplus, because a plate that yields
    them still over-produces.

    ⚠️ The third value is why the guard is not silent. A stopped plan looks
    exactly like a finished one — rows, totals, an empty ``unsatisfiable`` —
    and the operator would print it believing it covers the order. Reading it
    as "the loop ended by exhaustion" also flags the boundary case where the
    LAST iteration happened to finish the job; telling those two apart costs a
    scan that buys nothing, and at ten thousand prints "this is more than one
    plan shows" is not a lie.
    """
    remaining = {pid: n for pid, n in outstanding.items() if n > 0}
    yields = {plate.id: line_yield(recipe, counted) for plate, _file, recipe in candidates}
    # Beside ``yields`` and for the same reason: the tie-break reads it on every
    # candidate of every iteration, and it is a pure function of the recipe. Left
    # inside the loop it was recomputed ``MAX_ITERATIONS × len(candidates)``
    # times for an answer that cannot have changed.
    seconds = {plate.id: estimate_seconds(recipe) for plate, _file, recipe in candidates}
    rows: dict[int, PlanRow] = {}
    order: list[int] = []
    truncated = True
    for _ in range(MAX_ITERATIONS):
        best: tuple[tuple, ProductPlate, LibraryFile, PlateRecipe, dict[int, int]] | None = None
        for plate, file, recipe in candidates:
            gain = {pid: min(n, remaining.get(pid, 0)) for pid, n in yields[plate.id].items()}
            useful = sum(gain.values())
            if useful <= 0:
                continue
            waste = sum(max(0, n - remaining.get(pid, 0)) for pid, n in yields[plate.id].items())
            key = _pick_key(useful, waste, seconds[plate.id], plate.id)
            if best is None or key < best[0]:
                best = (key, plate, file, recipe, gain)
        if best is None:
            truncated = False
            break  # nothing left that covers anything: the plan is complete
        _key, plate, file, recipe, gain = best
        row = rows.get(plate.id)
        if row is None:
            row = rows[plate.id] = _row_for(plate, file, recipe, price_per_gram)
            order.append(plate.id)
        row.count += 1
        for pid, n in gain.items():
            if not n:
                continue
            row.useful[pid] = row.useful.get(pid, 0) + n
            remaining[pid] = max(0, remaining[pid] - n)
    planned = [rows[plate_id] for plate_id in order]
    surplus = {}
    for pid, want in outstanding.items():
        made = sum(row.count * yields[row.plate_id].get(pid, 0) for row in planned)
        if made > want:
            surplus[pid] = made - want
    return planned, surplus, truncated


def _totals(lines: list[LinePlan], price_per_gram: float | None) -> PlanTotals:
    """Time is ``None`` the moment one row has no estimate — a partial sum would
    read as a promise. Grams sum what is known: a row with no figure contributes
    nothing rather than voiding the column.

    ``cost`` is ``None`` unless a rate exists AND at least one row could be
    costed. A farm that has entered a rate but plans only plates with no weight
    would otherwise read 0.00, i.e. "this plan is free" — the same lie
    ``filament_cost.cost_of`` refuses to tell, and the same reason time voids
    itself rather than reporting the half it knows.
    """
    totals = PlanTotals()
    seconds = 0
    unknown = False
    grams = 0.0
    cost = 0.0
    costed = False
    for line in lines:
        for row in line.rows:
            totals.prints += row.count
            if row.print_time_seconds is None:
                unknown = True
            else:
                seconds += row.count * row.print_time_seconds
            if row.filament_used_grams is not None:
                grams += row.count * row.filament_used_grams
            if row.cost is not None:
                cost += row.count * row.cost
                costed = True
    totals.print_time_seconds = None if unknown else seconds
    totals.filament_used_grams = round(grams, 2)
    totals.cost = round(cost, 2) if price_per_gram is not None and costed else None
    return totals


def plan_lines(
    ctx: OrderContext,
    figures: dict[int, LineFigures],
    recipes_by_product: dict[int, list[Candidate]],
    queued: dict[int, dict[int, int]],
    price_per_gram: float | None,
) -> OrderPlan:
    """The plan for every line of the order. Pure — see the module docstring.

    ``outstanding = max(0, remaining − in_progress − queued)`` per counted part
    (spec decision 2): what the archives still owe, minus what is on a printer
    right now, minus what somebody has already queued for this line. Unsliced
    plates are reported under ``not_sliced`` and never planned; a part with
    outstanding work that no candidate plate yields at all is ``unsatisfiable``
    — note that a part the iteration guard simply ran out of prints for is NOT,
    because the guard is a defence and not a verdict about the plate. That the
    guard tripped at all is said once, for the whole order, in
    :attr:`OrderPlan.truncated`.

    ⚠️ The ``max(0, …)`` floor is not decoration: more queued than remaining is
    an ordinary state (somebody queued a plate that over-produces), and a
    negative outstanding would flow straight into the surplus arithmetic as a
    phantom.

    The names ride out with the plan (see :class:`OrderPlan`) — they come from
    the context this function was handed, so no caller need re-read them.
    """
    plans: list[LinePlan] = []
    truncated = False
    for line in ctx.lines:
        figs = figures.get(line.id)
        parts = list(figs.parts) if figs is not None else []
        counted = {pf.part_id for pf in parts}
        already = queued.get(line.id) or {}
        outstanding = {pf.part_id: max(0, pf.remaining - pf.in_progress - already.get(pf.part_id, 0)) for pf in parts}
        recipes = recipes_by_product.get(line.product_id) or []
        # An unsliced plate has no filaments to match a material against, so it
        # is listed whole — the operator slices it and it becomes a candidate.
        not_sliced = [plate.id for plate, _file, recipe in recipes if not recipe.sliced]
        candidates = [row for row in recipes if row[2].sliced and _material_ok(line.material, row[2].materials)]
        rows, surplus, line_truncated = cover(outstanding, candidates, counted, price_per_gram)
        truncated = truncated or line_truncated
        yielded: set[int] = set()
        for _plate, _file, recipe in candidates:
            yielded |= set(line_yield(recipe, counted))
        plans.append(
            LinePlan(
                line_id=line.id,
                product_id=line.product_id,
                material=line.material,
                outstanding_before={pid: n for pid, n in sorted(outstanding.items()) if n > 0},
                rows=rows,
                surplus_after=surplus,
                unsatisfiable=sorted(pid for pid, n in outstanding.items() if n > 0 and pid not in yielded),
                candidates=[plate.id for plate, _file, _recipe in candidates],
                not_sliced=not_sliced,
            )
        )
    return OrderPlan(
        lines=plans,
        totals=_totals(plans, price_per_gram),
        part_names={part.id: part.name for parts in ctx.parts_by_product.values() for part in parts},
        product_names={pid: product.name for pid, product in ctx.products_by_id.items()},
        truncated=truncated,
    )


async def queued_yield_by_line(
    db: AsyncSession,
    recipes_by_product: dict[int, list[Candidate]],
    lines: list[ProjectLine],
    counted_by_line: dict[int, set[int]],
) -> dict[int, dict[int, int]]:
    """How much each line already has coming: ``line_id → part_id → parts queued``.

    ⚠️ **Counted parts only, exactly as :func:`line_yield` counts them.** A
    queued plate is shared like any other (two products' lids on one bed, a
    part the product zeroes with ``qty_per_unit = 0``), and the parts of it
    this line does not count are not this line's incoming work. The map went
    out keyed on the RAW ``yield_by_part``, so it carried entries no figure of
    this line ever mentions — harmless in the subtraction below (an uncounted
    id matches no ``PartFigures``) and wrong for everybody else, because the
    map is public. ``counted_by_line`` is the line's ``PartFigures`` part ids,
    from the same ``attribute`` pass the plan is built on; a line missing from
    it counts nothing.

    "Still waiting" is the vault's rule, and it differs per table:
    ``print_queue.status == 'pending'``, and ``auto_queue_items.status ==
    'pending' AND assigned_to_item_id IS NULL`` — an auto row that has already
    been handed to a printer item is counted once, through that item.

    A row's plate is resolved the way attribution resolves an archive's: the
    exact ``(library_file_id, plate_id or 0)`` plate of the line's product
    first, then that product's whole-file (``plate_index = 0``) plate, which
    claims every plate of its file. ⚠️ ``plate_id`` on a queue row is the plate
    INDEX, not a ``ProductPlate.id``.

    ⚠️ A row whose file belongs to no plate of the line's product counts
    NOTHING — the file was unlinked, or the row points somewhere else entirely,
    and there is no yield to invent. Same for a row queued from an archive
    rather than a library file (``library_file_id IS NULL``): every writer the
    plan itself uses stamps the file, so the plan's own work always round-trips.

    ⚠️ **The order is never named here, on purpose.** The filter is on the LINE
    ids, which already scope it, and a queue row may carry ``project_line_id``
    without ``project_id`` and must still count — so an order filter would drop
    real work. The parameter that used to say so was passed and never read;
    naming it in this docstring is the whole of it.

    ⚠️ Both reads select THREE COLUMNS, never the entity. Loading an
    ``AutoQueueItem`` drags its ``target_location`` in on ``lazy="selectin"``,
    and a ``printer_locations`` SELECT inside the plan path is a lie about what
    this code does: a reader grepping the planner for "printer" must find
    nothing, because routing is not dispatching. The three columns are also all
    the yield lookup needs.
    """
    out: dict[int, dict[int, int]] = {line.id: {} for line in lines}
    if not out:
        return out
    product_by_line = {line.id: line.product_id for line in lines}
    exact: dict[int, dict[tuple[int, int], PlateRecipe]] = {}
    whole_file: dict[int, dict[int, PlateRecipe]] = {}
    for product_id, rows in recipes_by_product.items():
        exact[product_id] = {(p.library_file_id, p.plate_index): r for p, _f, r in rows}
        whole_file[product_id] = {p.library_file_id: r for p, _f, r in rows if p.plate_index == 0}
    line_ids = list(out)
    waiting = list(
        (
            await db.execute(
                select(PrintQueueItem.project_line_id, PrintQueueItem.library_file_id, PrintQueueItem.plate_id).where(
                    PrintQueueItem.project_line_id.in_(line_ids), PrintQueueItem.status == "pending"
                )
            )
        ).all()
    ) + list(
        (
            await db.execute(
                select(AutoQueueItem.project_line_id, AutoQueueItem.library_file_id, AutoQueueItem.plate_id).where(
                    AutoQueueItem.project_line_id.in_(line_ids),
                    AutoQueueItem.status == "pending",
                    AutoQueueItem.assigned_to_item_id.is_(None),
                )
            )
        ).all()
    )
    for line_id, library_file_id, plate_id in waiting:
        if library_file_id is None:
            continue
        product_id = product_by_line.get(line_id)
        if product_id is None:
            continue
        recipe = exact.get(product_id, {}).get((library_file_id, plate_id or 0)) or whole_file.get(product_id, {}).get(
            library_file_id
        )
        if recipe is None:
            continue
        bucket = out[line_id]
        for part_id, n in line_yield(recipe, counted_by_line.get(line_id) or set()).items():
            bucket[part_id] = bucket.get(part_id, 0) + n
    return out


async def plan_for_order(db: AsyncSession, project_id: int) -> OrderPlan | None:
    """Load everything :func:`plan_lines` needs and run it. ``None`` = no order.

    Computed on every read, never cached and never stored: a second call after
    enqueuing sees the new queue rows and plans that much less.
    """
    ctx = await load_order_context(db, project_id)
    if ctx is None:
        return None
    figures, _other = attribute(ctx)
    # ⚠️ ONE load for the whole order. This was a comprehension calling the
    # single-product helper per product — a SELECT per line of a page that
    # recomputes its plan on every read.
    recipes_by_product = await recipes_for_products(db, ctx.products_by_id.values())
    counted_by_line = {line_id: {pf.part_id for pf in figs.parts} for line_id, figs in figures.items()}
    queued = await queued_yield_by_line(db, recipes_by_product, ctx.lines, counted_by_line)
    rate_per_kg = await default_rate_per_kg(db)
    price_per_gram = rate_per_kg / 1000.0 if rate_per_kg > 0 else None
    return plan_lines(ctx, figures, recipes_by_product, queued, price_per_gram)


__all__ = [
    "MAX_ITERATIONS",
    "Candidate",
    "LinePlan",
    "OrderPlan",
    "PlanRow",
    "PlanTotals",
    "cover",
    "line_yield",
    "plan_for_order",
    "plan_lines",
    "queued_yield_by_line",
]
