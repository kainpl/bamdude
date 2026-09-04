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
session, no printer state, no clock. Only :func:`plan_for_orders` (and its
one-order wrapper :func:`plan_for_order`) and :func:`queued_yield_by_line` touch
the database. Routing is not dispatching — nothing here asks whether a printer
is ready, and nothing here may start to.

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
from backend.app.services.order_filing import line_for_plate
from backend.app.services.order_metrics import (
    LineFigures,
    OrderContext,
    attribute,
    batch_contexts,
    line_accepts_materials,
)
from backend.app.services.product_composition import PlateRecipe, estimate_seconds, recipes_for_products

# A defence, not a feature: a plate whose yield somehow never shrinks the
# outstanding map would otherwise spin forever inside a request.
MAX_ITERATIONS = 10_000

Candidate = tuple[ProductPlate, LibraryFile, PlateRecipe]


@dataclass
class PlanAlternative:
    """Another plate of the same line that makes exactly the same counted parts.

    The same part is routinely sliced once per printer model — two files, one
    yield — and the greedy picks whichever scored best and hangs every print on
    it, so the other file was invisible in the plan block (user report,
    2026-09-04). This is that other file, riding out on the row it duplicates.

    ⚠️ **"The same" is the COUNTED yield**, exactly as :func:`line_yield` reads
    it: a plate carrying an extra part this line does not count still makes the
    same thing for this line, and a plate making half as many is a different
    plate that belongs in the "+ plate" menu instead.

    The figures are PER PRINT, like a row's — the block re-does its arithmetic
    against them the moment the operator switches files, without asking the
    server again. The count does NOT move with the switch: the yields are
    identical by construction, so the same number of prints covers the same
    work.
    """

    plate_id: int  # ProductPlate.id
    library_file_id: int
    plate_index: int  # 0 = the whole file
    filename: str
    printer_model: str | None = None
    print_time_seconds: int | None = None
    filament_used_grams: float | None = None
    cost: float | None = None
    time_unknown: bool = False


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
    # The printer model this plate's file was sliced for, in the spelling the
    # auto-queue routes on. ``None`` = the file names none.
    printer_model: str | None = None
    # The line's OTHER candidate plates with the identical counted yield, this
    # one excluded, sorted by ``(printer_model or "", plate_id)``. Empty is the
    # ordinary case; see :class:`PlanAlternative`.
    alternatives: list[PlanAlternative] = field(default_factory=list)


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


def _figures(recipe: PlateRecipe, price_per_gram: float | None) -> tuple[int | None, float | None, float | None]:
    """``(seconds, grams, cost)`` for ONE print of this plate.

    Shared by :func:`_row_for` and :func:`_alternative_for` because a row and
    the alternative that replaces it on screen are priced by the same rule — the
    block swaps one for the other and the operator must not see the cost of a
    plate change its arithmetic along with its file.
    """
    grams = recipe.filament_used_grams
    secs = estimate_seconds(recipe)
    return secs, grams, (round(grams * price_per_gram, 2) if grams and price_per_gram else None)


def _row_for(plate: ProductPlate, file: LibraryFile, recipe: PlateRecipe, price_per_gram: float | None) -> PlanRow:
    secs, grams, cost = _figures(recipe, price_per_gram)
    return PlanRow(
        plate_id=plate.id,
        library_file_id=plate.library_file_id,
        plate_index=plate.plate_index,
        filename=file.filename,
        print_time_seconds=secs,
        filament_used_grams=grams,
        cost=cost,
        time_unknown=secs is None,
        printer_model=recipe.printer_model,
    )


def _alternative_for(
    plate: ProductPlate, file: LibraryFile, recipe: PlateRecipe, price_per_gram: float | None
) -> PlanAlternative:
    secs, grams, cost = _figures(recipe, price_per_gram)
    return PlanAlternative(
        plate_id=plate.id,
        library_file_id=plate.library_file_id,
        plate_index=plate.plate_index,
        filename=file.filename,
        printer_model=recipe.printer_model,
        print_time_seconds=secs,
        filament_used_grams=grams,
        cost=cost,
        time_unknown=secs is None,
    )


def _attach_alternatives(
    rows: list[PlanRow], candidates: list[Candidate], counted: set[int], price_per_gram: float | None
) -> None:
    """Hang each row's interchangeable plates on it, in place.

    Runs AFTER :func:`cover` and changes nothing it decided: the pick, the
    counts and the surplus are the picked plate's, and swapping a file is the
    block's what-if, not a second plan. It costs one pass over the candidates
    the line already had — no query, no recipe re-read.

    The grouping key is the plate's yield of COUNTED parts as a frozen set of
    ``(part_id, n)`` pairs, i.e. :func:`line_yield` frozen. Two plates share a
    key exactly when one print of either covers the same work for this line;
    everything else about them — the file, the printer model, the time, the
    weight — is free to differ, and that is the point.
    """
    if not rows:
        return
    by_key: dict[frozenset[tuple[int, int]], list[Candidate]] = {}
    key_by_plate: dict[int, frozenset[tuple[int, int]]] = {}
    for plate, file, recipe in candidates:
        key = frozenset(line_yield(recipe, counted).items())
        key_by_plate[plate.id] = key
        by_key.setdefault(key, []).append((plate, file, recipe))
    for row in rows:
        key = key_by_plate.get(row.plate_id)
        if key is None:
            continue
        row.alternatives = sorted(
            (
                _alternative_for(plate, file, recipe, price_per_gram)
                for plate, file, recipe in by_key[key]
                if plate.id != row.plate_id
            ),
            key=lambda alt: (alt.printer_model or "", alt.plate_id),
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
        candidates = [row for row in recipes if row[2].sliced and line_accepts_materials(line, row[2].materials)]
        rows, surplus, line_truncated = cover(outstanding, candidates, counted, price_per_gram)
        _attach_alternatives(rows, candidates, counted, price_per_gram)
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

    ⚠️ **Two branches, and only the second one names the order.** The LINE
    branch filters on ``project_line_id`` alone, because a queue row may carry a
    line without an order id and must still count — naming the order THERE would
    drop real work. The IMPLICIT branch is its opposite and exists for the rows
    the other one cannot see: ``project_id == this order AND project_line_id IS
    NULL`` (spec pass 7, Decision 4b — the legacy, Telegram and hand-written
    rows the writers' own filing does not reach). It resolves the line itself,
    plate → product → material, through the same ``order_filing.line_for_plate``
    the writers call, and a row that resolves to no line of its order counts
    NOWHERE: an ambiguous product is not a licence to guess. The two branches
    are disjoint by construction (``project_line_id IN lines`` vs ``IS NULL``)
    and add into the same buckets, so a row is counted once or not at all.

    ⚠️ Both reads select THREE COLUMNS, never the entity. Loading an
    ``AutoQueueItem`` drags its ``target_location`` in on ``lazy="selectin"``,
    and a ``printer_locations`` SELECT inside the plan path is a lie about what
    this code does: a reader grepping the planner for "printer" must find
    nothing, because routing is not dispatching. The columns are also all the
    yield lookup needs — the implicit branch's material test reads
    ``PlateRecipe.materials``, which the recipes already carry, so naming the
    order costs no query and no file read.
    """
    out: dict[int, dict[int, int]] = {line.id: {} for line in lines}
    if not out:
        return out
    product_by_line = {line.id: line.product_id for line in lines}
    lines_by_project: dict[int, list[ProjectLine]] = {}
    for line in lines:
        lines_by_project.setdefault(line.project_id, []).append(line)
    # Keyed by PLATE, not by product: the line branch asks "this product's
    # recipe for this plate" and the implicit branch asks "whose plate is this",
    # and one pair of maps answers both. ``by_plate`` is the exact index,
    # ``by_file`` the whole-file (index 0) plate that claims every plate of its
    # file; exact wins wherever both exist for the same product.
    by_plate: dict[tuple[int, int], dict[int, PlateRecipe]] = {}
    by_file: dict[int, dict[int, PlateRecipe]] = {}
    for product_id, rows in recipes_by_product.items():
        for plate, _file, recipe in rows:
            by_plate.setdefault((plate.library_file_id, plate.plate_index), {})[product_id] = recipe
            if plate.plate_index == 0:
                by_file.setdefault(plate.library_file_id, {})[product_id] = recipe

    def _recipes_for(library_file_id: int, plate_index: int) -> dict[int, PlateRecipe]:
        return {**by_file.get(library_file_id, {}), **by_plate.get((library_file_id, plate_index), {})}

    def _count(line_id: int, recipe: PlateRecipe) -> None:
        bucket = out[line_id]
        for part_id, n in line_yield(recipe, counted_by_line.get(line_id) or set()).items():
            bucket[part_id] = bucket.get(part_id, 0) + n

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
        recipe = _recipes_for(library_file_id, plate_id or 0).get(product_id)
        if recipe is None:
            continue
        _count(line_id, recipe)

    project_ids = list(lines_by_project)
    unfiled = list(
        (
            await db.execute(
                select(PrintQueueItem.project_id, PrintQueueItem.library_file_id, PrintQueueItem.plate_id).where(
                    PrintQueueItem.project_id.in_(project_ids),
                    PrintQueueItem.project_line_id.is_(None),
                    PrintQueueItem.status == "pending",
                )
            )
        ).all()
    ) + list(
        (
            await db.execute(
                select(AutoQueueItem.project_id, AutoQueueItem.library_file_id, AutoQueueItem.plate_id).where(
                    AutoQueueItem.project_id.in_(project_ids),
                    AutoQueueItem.project_line_id.is_(None),
                    AutoQueueItem.status == "pending",
                    AutoQueueItem.assigned_to_item_id.is_(None),
                )
            )
        ).all()
    )
    for project_id, library_file_id, plate_id in unfiled:
        if library_file_id is None:
            continue
        order_lines = lines_by_project.get(project_id) or []
        hits: dict[int, PlateRecipe] = {}
        # ⚠️ **The material test here can be WIDER than the writers'.** They ask
        # ``plate_materials(meta, the row's own plate index)``; this asks
        # ``PlateRecipe.materials``, which is built from the PRODUCT PLATE's
        # index — and for a whole-file plate (index 0) on a multi-plate 3MF that
        # is the union of every plate's filaments. So a PETG-only line can accept
        # a row here whose actual plate prints PLA, where ``resolve_line_id``
        # would have left it unfiled. The divergence is deliberate and one-way:
        # this branch exists for rows nobody filed, and a whole-file plate is the
        # product saying "this file makes my parts" without saying which plate
        # does — narrowing it per row would drop the legacy rows this branch was
        # added to count. A row the writers DID file never reaches here at all;
        # the two branches are disjoint.
        for product_id, recipe in _recipes_for(library_file_id, plate_id or 0).items():
            line = line_for_plate(order_lines, product_id, recipe.materials)
            if line is not None:
                hits[line.id] = recipe
        # Two products of the same order each claiming a line of their own is as
        # unanswerable as two lines of one product; both end here, counting
        # nowhere.
        if len(hits) != 1:
            continue
        ((line_id, recipe),) = hits.items()
        _count(line_id, recipe)
    return out


async def plan_for_orders(db: AsyncSession, project_ids: list[int]) -> dict[int, OrderPlan]:
    """The plan of every order in ``project_ids``, in ONE round of queries.

    ``project_id → OrderPlan``, with no entry for an id that names no order —
    the same "there is nothing here" :func:`plan_for_order` says with ``None``.

    ⚠️ **This is the batch, and the one-order function is a wrapper over it.**
    The candidates endpoint asks for the plan of every order that could hold a
    plate, which on a working farm is every open order carrying that product; in
    a loop that was ~14 statements EACH, including a full archive-and-parts read
    per order. ``order_metrics.batch_contexts`` is the loader the orders list
    already uses for exactly this shape, and it returns the contexts
    :func:`load_order_context` would have returned one at a time (pinned by its
    own parity test), so the arithmetic below is untouched.

    Two things are shared across the orders and both are safe to share:

    * ``recipes_for_products`` over the UNION of every order's products — a
      recipe is a property of a plate and a file, not of the order asking, and
      ``plan_lines`` indexes it by ``line.product_id``. A superset costs nothing.
    * ``queued_yield_by_line`` over the UNION of the lines. Its LINE branch
      filters on line ids, and its IMPLICIT branch groups the lines it was given
      by ``project_id`` and asks each order about its own rows — so both branches
      stay keyed per order however many orders are in the call. A product of
      SOMEBODY ELSE's order in the shared recipe map yields no line here either:
      ``line_for_plate`` only ever looks at the lines of the row's own order.

    Computed on every read, never cached and never stored: a second call after
    enqueuing sees the new queue rows and plans that much less.
    """
    if not project_ids:
        return {}
    contexts = await batch_contexts(db, project_ids)
    if not contexts:
        return {}
    figures_by_project = {ctx.project.id: attribute(ctx)[0] for ctx in contexts}
    # ⚠️ ONE load for every product of every order. Per ORDER this was already a
    # single call (it used to be one per product — a SELECT per line of a page
    # that recomputes its plan on every read); per BATCH it is one for all.
    products_by_id = {pid: product for ctx in contexts for pid, product in ctx.products_by_id.items()}
    recipes_by_product = await recipes_for_products(db, products_by_id.values())
    counted_by_line = {
        line_id: {pf.part_id for pf in figs.parts}
        for figures in figures_by_project.values()
        for line_id, figs in figures.items()
    }
    all_lines = [line for ctx in contexts for line in ctx.lines]
    queued = await queued_yield_by_line(db, recipes_by_product, all_lines, counted_by_line)
    rate_per_kg = await default_rate_per_kg(db)
    price_per_gram = rate_per_kg / 1000.0 if rate_per_kg > 0 else None
    return {
        ctx.project.id: plan_lines(ctx, figures_by_project[ctx.project.id], recipes_by_product, queued, price_per_gram)
        for ctx in contexts
    }


async def plan_for_order(db: AsyncSession, project_id: int) -> OrderPlan | None:
    """One order's plan — :func:`plan_for_orders` for one. ``None`` = no order."""
    return (await plan_for_orders(db, [project_id])).get(project_id)


__all__ = [
    "MAX_ITERATIONS",
    "Candidate",
    "LinePlan",
    "OrderPlan",
    "PlanAlternative",
    "PlanRow",
    "PlanTotals",
    "cover",
    "line_yield",
    "plan_for_order",
    "plan_for_orders",
    "plan_lines",
    "queued_yield_by_line",
]
