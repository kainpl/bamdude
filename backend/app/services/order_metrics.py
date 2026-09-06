"""Archive → order-line attribution and every order figure (spec §Fact attribution).

The archive is the only source of fact. Queue tables are never read here.
Everything is computed on read over one loaded ``OrderContext``; an order has
tens to hundreds of archives, which is nothing.

That last sentence is about ONE order. The list pages ask about many at once
(``batch_contexts`` → :func:`grouped_figures`), where "tens to hundreds of
archives" is multiplied by the page — which is why that loader is batched
rather than looped, why its ``IN`` lists are chunked, and why its parity with
:func:`load_order_context` is pinned by a test instead of by inspection.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine, ProjectProcurement
from backend.app.services.product_composition import part_index

if TYPE_CHECKING:  # the runtime import would close the circle part_stock already opens
    from backend.app.services.part_stock import LineStockReads

_DONE = "completed"
_RUNNING = "printing"

#: How many ids go into one ``IN (...)`` list. The same 500 the library scanner
#: batches in (m148) and the same ``selectin`` uses for its parent chunking, so
#: the batch loader's statement count is a multiple of one number and not two.
#: Public because ``part_stock`` chunks the reservation read it does FOR this
#: module's loaders — two constants both spelled 500 would drift the first time
#: somebody tuned one of them.
IN_CHUNK = 500


def archive_material_set(filament_type: str | None) -> set[str]:
    """``PrintArchive.filament_type`` is a joined string ("PLA, PETG")."""
    return {tok.strip().upper() for tok in (filament_type or "").split(",") if tok.strip()}


def index_plates(plates: Iterable[ProductPlate]) -> tuple[dict[tuple[int, int], list[int]], dict[int, list[int]]]:
    """``(file, plate) → product ids`` and ``file → product ids`` for the 0-rows.

    The two halves of :attr:`OrderContext.plate_product` /
    :attr:`OrderContext.whole_file_product`, built once so the per-order loader,
    the batch loader and ``part_stock``'s order-less credit cannot drift about
    what a plate belongs to. Ids come back sorted, which is what makes "the
    first product that counts this part" a stable answer rather than whatever
    the set iterated to.
    """
    exact: dict[tuple[int, int], set[int]] = defaultdict(set)
    whole_file: dict[int, set[int]] = defaultdict(set)
    for plate in plates:
        exact[(plate.library_file_id, plate.plate_index)].add(plate.product_id)
        if plate.plate_index == 0:
            whole_file[plate.library_file_id].add(plate.product_id)
    return (
        {key: sorted(pids) for key, pids in exact.items()},
        {file_id: sorted(pids) for file_id, pids in whole_file.items()},
    )


def products_for_print(
    plate_product: dict[tuple[int, int], list[int]],
    whole_file_product: dict[int, list[int]],
    *,
    library_file_id: int | None,
    plate_index: int | None,
) -> list[int]:
    """Which products a printed plate belongs to: exact plate first, then the
    whole-file wildcard.

    A product plate with ``plate_index = 0`` claims EVERY plate of that file,
    which is how a single-plate file (one 0-row from the sync) meets its prints,
    which carry the slicer's index, 1. Without the second lookup those two
    numbers never meet. **Not a union**: a file whose plate 2 belongs to one
    product and whose 0-row belongs to another answers with the plate's owner
    only, because the exact link is the more specific statement.
    """
    return plate_product.get((library_file_id, plate_index or 0)) or whole_file_product.get(library_file_id) or []


@dataclass
class PartFigures:
    """One counted part of one line.

    ``need`` is what is still to be PRINTED — the reservation lowers it
    (Decision 5). ``surplus`` is measured against the FULL quantity instead
    (Ruling 24): what the shelf lent this line is not surplus, it is a loan,
    and it comes back through a release. Counting it in both places is what let
    an operator bank the raised surplus and then release the reservation, ending
    with two more kits than the farm ever made.

    ``already_banked`` is Σ ``surplus_banked`` for this (line, part) and
    ``bankable`` the part of the surplus still to move — one computation, read
    by the button's gate and by the button itself (Ruling 30).
    """

    part_id: int
    name: str
    kind: str
    qty_per_unit: int
    need: int = 0
    usable: int = 0
    in_progress: int = 0
    remaining: int = 0
    surplus: int = 0
    already_banked: int = 0
    bankable: int = 0


@dataclass
class LineFigures:
    """One order line's figures.

    ``from_stock_units`` is how many whole units the line took off the
    product's free stock (pass 8, Decision 4) — read from the ledger, never a
    column. It is the RAW ledger reading and is deliberately not capped at
    ``quantity``: the shelf gave up that many kits whatever the line was later
    edited to, and a capped twin would disagree with the product's balance. The
    arithmetic that cannot go negative floors itself instead (see
    :func:`_new_line_figures`).
    """

    line_id: int
    product_id: int
    quantity: int
    material: str | None
    units_printed: int = 0
    from_stock_units: int = 0
    progress: float = 0.0
    parts: list[PartFigures] = field(default_factory=list)
    archive_ids: list[int] = field(default_factory=list)
    # Live counters (spec 2026-09-06, Slice C): running archives attributed to
    # this line, and pending queue rows stamped with this line.
    prints_in_progress: int = 0
    prints_queued: int = 0


@dataclass
class ProcurementFigures:
    part_id: int
    name: str
    need: int
    acquired: int
    remaining: int


@dataclass
class ProjectFigures:
    ordered: int = 0
    printed: int = 0
    complete: int = 0
    remaining: int = 0
    total_time_seconds: int = 0
    total_filament_grams: float = 0.0
    total_cost: float = 0.0
    defective: int = 0
    margin: float | None = None
    progress: float = 0.0
    other_prints_count: int = 0
    all_printed: bool = False
    #: Σ of the lines' ``from_stock_units`` — units this order took off the
    #: shelf instead of printing (pass 8, Decision 5). ``printed`` stays prints
    #: only; ``remaining``, ``progress``, ``complete`` and ``all_printed``
    #: count these as done, because they are.
    from_stock_units: int = 0
    #: Σ over every line and part of ``PartFigures.bankable`` — what
    #: «Списати надлишок» would move if it were pressed now (Ruling 30). The
    #: button is enabled on exactly this, so the gate and the action cannot
    #: disagree: it gated on ``surplus`` before, which banking never lowers, and
    #: the button stayed lit for ever over a shelf nothing more was going onto.
    bankable_surplus: int = 0
    #: Archives in ``printing`` under this order, and pending rows of both queue
    #: tiers under it — rows on a line AND rows filed under the order alone.
    prints_in_progress: int = 0
    prints_queued: int = 0


@dataclass
class OrderContext:
    project: Project
    lines: list[ProjectLine]
    products_by_id: dict[int, Product]
    parts_by_product: dict[int, list[ProductPart]]
    # A plate names a SET of products, not one (spec §Data model, "A plate
    # linked to several products is normal"): a shared file sits in several
    # products, and one bed may carry two products' parts. While these indexes
    # held a single id, whichever product loaded last silently took every print.
    plate_product: dict[tuple[int, int], list[int]]  # (library_file_id, plate_index) → product ids
    archives: list[PrintArchive]  # not trashed, oldest first
    archive_parts_by_archive: dict[int, list[PrintArchivePart]]
    procurement_by_part: dict[int, int]  # product_part_id → quantity_acquired
    # ``plate_index = 0`` on a product plate means THE WHOLE FILE (spec §Data
    # model, Conventions): every plate of that file matches it. Kept as its own
    # index because the two sides of the join count plates differently — a
    # single-plate file gets a 0-row from ``wanted_plate_indices``, while its
    # archives carry the slicer's own index, which is 1. An exact tuple lookup
    # therefore misses nearly every single-plate print there is.
    whole_file_product: dict[int, list[int]] = field(default_factory=dict)  # library_file_id → product ids
    # ``line_id → kits reserved from the product's free stock`` (pass 8,
    # Decision 4). Loaded, not derived: the reservation lives in the stock
    # ledger and nowhere else, so a context built without it reads every line
    # as reserving nothing — which is exactly right for the tests that build
    # one by hand and for an order whose product has no stock.
    reserved_by_line: dict[int, int] = field(default_factory=dict)
    # ``(line_id, part_id) → Σ surplus_banked`` (Ruling 30). Comes off the SAME
    # grouped read as ``reserved_by_line`` — see ``part_stock.line_ledger_reads``
    # — because the movements table is pinned to one statement per response.
    # Empty means "nothing has been banked yet", which is what a hand-built
    # context should read.
    banked_by_line_part: dict[tuple[int, int], int] = field(default_factory=dict)
    # ``line_id → pending queue rows on that line`` and the rows under the order
    # with no line, both tiers summed (an auto row already handed to a printer
    # item is counted through that item — ``queued_yield_by_line``'s rule).
    queued_by_line: dict[int, int] = field(default_factory=dict)
    queued_unfiled: int = 0


def _counted_qty_per_unit(products: Iterable[Product]) -> dict[int, int]:
    """``part_id → qty_per_unit`` over the counted parts of these products.

    The divisor the stock ledger's reservation is read back through, taken from
    the parts the loader has already got rather than re-queried. Same predicate
    as :func:`_new_line_figures` and ``part_stock.is_counted``.
    """
    return {
        part.id: part.qty_per_unit
        for product in products
        for part in product.parts
        if part.kind == "printed" and part.qty_per_unit > 0
    }


async def _load_reserved(db: AsyncSession, line_ids: Sequence[int], qty_per_unit: dict[int, int]) -> LineStockReads:
    """:attr:`OrderContext.reserved_by_line` AND :attr:`OrderContext.banked_by_line_part`,
    in one query for every line given.

    ⚠️ Imported inside the function on purpose: ``part_stock`` imports THIS
    module for ``index_plates`` / ``products_for_print`` / ``row_quantity``, so
    a module-level import here would close the circle. By the time a loader
    runs, both modules are fully imported.

    Both loaders go through this one helper, so "the batch loader loads the
    reservation the same way" is structural rather than a promise — the same
    reason the archive-parts ordering is shared. It returns BOTH halves for the
    same reason: one statement over ``product_part_stock_movements`` per
    response, pinned by a spy on every list and detail endpoint that has one.
    """
    from backend.app.services.part_stock import line_ledger_reads

    return await line_ledger_reads(db, line_ids, qty_per_unit)


async def _load_queued(db: AsyncSession, project_ids: Sequence[int]) -> dict[int, dict[int | None, int]]:
    """``project_id → {line_id or None → pending rows}`` over both queue tiers,
    one statement per tier for every order asked about. The per-order loader
    and the batch loader both come through here, so a list row and the page it
    opens cannot disagree about what is waiting."""
    out: dict[int, dict[int | None, int]] = defaultdict(lambda: defaultdict(int))
    if not project_ids:
        return out
    for project_id, line_id, n in (
        await db.execute(
            select(PrintQueueItem.project_id, PrintQueueItem.project_line_id, func.count(PrintQueueItem.id))
            .where(PrintQueueItem.project_id.in_(project_ids), PrintQueueItem.status == "pending")
            .group_by(PrintQueueItem.project_id, PrintQueueItem.project_line_id)
        )
    ).all():
        out[project_id][line_id] += n
    for project_id, line_id, n in (
        await db.execute(
            select(AutoQueueItem.project_id, AutoQueueItem.project_line_id, func.count(AutoQueueItem.id))
            .where(
                AutoQueueItem.project_id.in_(project_ids),
                AutoQueueItem.status == "pending",
                AutoQueueItem.assigned_to_item_id.is_(None),
            )
            .group_by(AutoQueueItem.project_id, AutoQueueItem.project_line_id)
        )
    ).all():
        out[project_id][line_id] += n
    return out


async def load_order_context(db: AsyncSession, project_id: int) -> OrderContext | None:
    project = (
        await db.execute(select(Project).options(selectinload(Project.lines)).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        return None
    lines = sorted(project.lines, key=lambda line: (line.sort_order, line.id))
    product_ids = {line.product_id for line in lines}
    products = (
        (
            await db.execute(
                select(Product)
                .options(selectinload(Product.parts), selectinload(Product.plates))
                .where(Product.id.in_(product_ids))
            )
        )
        .scalars()
        .all()
        if product_ids
        else []
    )
    plate_products, whole_file_products = index_plates(plate for product in products for plate in product.plates)
    archives = (
        (
            await db.execute(
                select(PrintArchive)
                .where(PrintArchive.project_id == project_id, PrintArchive.deleted_at.is_(None))
                .order_by(PrintArchive.created_at, PrintArchive.id)
            )
        )
        .scalars()
        .all()
    )
    parts_rows = (
        (
            await db.execute(
                select(PrintArchivePart)
                .where(PrintArchivePart.archive_id.in_([a.id for a in archives]))
                # ⚠️ ``hand_out`` deals these rows out one at a time, so their
                # order decides which line gets a part when two both count it.
                # An unordered SELECT leaves that to the backend — and the batch
                # loader below reads the same table with a different filter, so
                # "the two loaders agree" was a property of the plan, not of the
                # code. Ordering both by id makes the parity structural.
                .order_by(PrintArchivePart.id)
            )
        )
        .scalars()
        .all()
        if archives
        else []
    )
    by_archive: dict[int, list[PrintArchivePart]] = defaultdict(list)
    for row in parts_rows:
        by_archive[row.archive_id].append(row)
    procurement = {
        row.product_part_id: row.quantity_acquired
        for row in (
            await db.execute(select(ProjectProcurement).where(ProjectProcurement.project_id == project_id))
        ).scalars()
    }
    reads = await _load_reserved(db, [line.id for line in lines], _counted_qty_per_unit(products))
    queued = (await _load_queued(db, [project_id])).get(project_id, {})
    return OrderContext(
        project=project,
        lines=lines,
        products_by_id={p.id: p for p in products},
        parts_by_product={p.id: list(p.parts) for p in products},
        plate_product=plate_products,
        archives=list(archives),
        archive_parts_by_archive=dict(by_archive),
        procurement_by_part=procurement,
        whole_file_product=whole_file_products,
        reserved_by_line=reads.reserved_units,
        banked_by_line_part=reads.banked_by_part,
        queued_by_line={lid: n for lid, n in queued.items() if lid is not None},
        queued_unfiled=queued.get(None, 0),
    )


def _new_line_figures(
    line: ProjectLine,
    parts: list[ProductPart],
    from_stock_units: int = 0,
    banked: Mapping[tuple[int, int], int] | None = None,
) -> LineFigures:
    """The line's parts, each with the number of them the ORDER still wants.

    ``need = (quantity − from_stock_units) × qty_per_unit`` (pass 8, Decision
    5): kits taken off the shelf are not printed twice, so every "still needed"
    figure downstream — ``remaining``, the plan's ``outstanding``, the greedy
    hand-out's ``_room`` — drops with the reservation.

    ⚠️ ``surplus`` deliberately does NOT rise with it (Ruling 24) — see
    :func:`_finish`. It is measured against the full quantity, because the kits
    the shelf lent this line go back to the shelf through a release and not
    through the banking button.

    ⚠️ The ``max(0, …)`` is not decoration: a line edited down to a quantity
    below what it has already reserved is an ordinary state, and a negative
    need would make ``remaining`` lie.
    """
    figs = LineFigures(
        line_id=line.id,
        product_id=line.product_id,
        quantity=line.quantity,
        material=line.material,
        from_stock_units=from_stock_units,
    )
    to_print = max(0, line.quantity - from_stock_units)
    for part in parts:
        if part.kind != "printed" or part.qty_per_unit <= 0:
            continue
        figs.parts.append(
            PartFigures(
                part_id=part.id,
                name=part.name,
                kind=part.kind,
                qty_per_unit=part.qty_per_unit,
                need=part.qty_per_unit * to_print,
                already_banked=(banked or {}).get((line.id, part.id), 0),
            )
        )
    return figs


def _units_printed(figs: LineFigures) -> int:
    if not figs.parts:
        return 0
    return min(p.usable // p.qty_per_unit for p in figs.parts)


def _finish(figs: LineFigures) -> None:
    # ⚠️ ``surplus`` is measured against the FULL quantity and ``remaining``
    # against ``need`` — the one figure the reservation does not move (Ruling
    # 24). A kit off the shelf lowers what is left to print; it does not make a
    # print that covers it "extra", because that kit is a loan the release gives
    # back. While the two shared ``need``, banking the raised surplus and then
    # releasing the reservation put the same kits on the shelf twice: 9 kits, a
    # 5-unit line holding 2 and 7 prints, and the shelf ended at 13 of a farm's
    # 16 with 5 shipped.
    for p in figs.parts:
        p.remaining = max(0, p.need - p.usable)
        p.surplus = max(0, p.usable - p.qty_per_unit * figs.quantity)
        p.bankable = max(0, p.surplus - p.already_banked)
    figs.units_printed = _units_printed(figs)
    # Capped on the wire: ``progress`` is what a bar fills from, and a bar
    # cannot be 300% full. The excess is not lost — ``units_printed`` and each
    # part's ``surplus`` carry it uncapped, which is where the overprint is
    # meant to be read. Clamping at every consumer instead was the alternative,
    # and the frontend was the only one that remembered.
    #
    # ``+ from_stock_units`` (pass 8, Decision 5): a unit taken off the shelf is
    # a unit the order has, so a fully reserved line reads 100 % with nothing
    # printed. ``units_printed`` stays prints only — the two numbers are shown
    # side by side and must not be one number that quietly means both.
    done = figs.units_printed + figs.from_stock_units
    figs.progress = min(1.0, round(done / figs.quantity, 4)) if figs.quantity else 0.0


def line_accepts_materials(line: ProjectLine, materials: set[str]) -> bool:
    """Does this line's material take a plate carrying ``materials``?

    A line with no material takes every plate; a line with one takes only plates
    that carry it. A plate whose materials are UNKNOWN (an empty set — an
    unsliced file, an archive with no filament type) therefore matches no
    constrained line: "we do not know" is not "it matches".

    ⚠️ **Public because it is the one material rule of the whole order half**,
    asked in three places that must agree: attribution (which line a finished
    archive belongs to), the plan engine (which plates a line may print) and
    ``order_filing.line_for_plate`` (which line a queued print is filed under).
    It used to exist as two private mirrors — ``_line_accepts`` here and
    ``plan_engine._material_ok`` — which is exactly one copy too many for a rule
    a fourth caller was about to need.
    """
    return line.material is None or line.material.strip().upper() in materials


def row_quantity(row: PrintArchivePart, status: str) -> int:
    """A completed row hands over what came out good; a running one its full count.

    Public because the order-less credit in ``part_stock`` asks the same
    question of the same rows: what a finished plate actually put on a shelf is
    what a finished plate would have put against a line.
    """
    if status == _DONE:
        return max(0, (row.quantity or 0) - (row.defective or 0))
    return row.quantity or 0


def _room(pf: PartFigures, status: str) -> int:
    """How many of this part the line still needs, never below zero. A print in
    progress competes with what is already on other printers; a finished one
    does not."""
    taken = pf.usable if status == _DONE else pf.usable + pf.in_progress
    return max(0, pf.need - taken)


def _award(pf: PartFigures, status: str, quantity: int) -> None:
    if status == _DONE:
        pf.usable += quantity
    else:
        pf.in_progress += quantity


def attribute(ctx: OrderContext) -> tuple[dict[int, LineFigures], list[PrintArchive]]:
    """Spec §Line resolution for an archive: the unit is the archive's PART ROW.

    One plate may carry parts of several products (two lids on one bed) and one
    file may sit in several products (a shared flask), so a single print can feed
    several lines — and did, wrongly, when it was handed out whole.
    """
    figures = {
        line.id: _new_line_figures(
            line,
            ctx.parts_by_product.get(line.product_id, []),
            ctx.reserved_by_line.get(line.id, 0),
            ctx.banked_by_line_part,
        )
        for line in ctx.lines
    }
    indexes = {pid: part_index(parts) for pid, parts in ctx.parts_by_product.items()}
    by_part_id = {line_id: {pf.part_id: pf for pf in figs.parts} for line_id, figs in figures.items()}
    line_by_id = {line.id: line for line in ctx.lines}

    def counted(line: ProjectLine, name_key: str) -> PartFigures | None:
        """This line's figures for that object, or None when its product does not
        count it — an object the product never heard of, or one it zeroed."""
        part = indexes.get(line.product_id, {}).get(name_key)
        return None if part is None else by_part_id[line.id].get(part.id)

    def candidates(archive: PrintArchive, exclude: ProjectLine | None = None) -> list[ProjectLine]:
        """The lines this print may feed, in ``sort_order``.

        The exact plate first, then the whole-file wildcard — see
        :func:`products_for_print`, which is that rule; without its second
        lookup every single-plate print lands in "other".
        """
        product_ids = products_for_print(
            ctx.plate_product,
            ctx.whole_file_product,
            library_file_id=archive.library_file_id,
            plate_index=archive.plate_index,
        )
        materials = archive_material_set(archive.filament_type)
        return [
            ln
            for ln in ctx.lines
            if ln.product_id in product_ids and ln is not exclude and line_accepts_materials(ln, materials)
        ]

    def hand_out(archive: PrintArchive, rows: list[PrintArchivePart], lines: list[ProjectLine]) -> set[int]:
        """Deal every part row out over ``lines`` greedily; return the ids of the
        lines that got something.

        Each row goes to the first line that counts the part and still needs it,
        the remainder to the next, and whatever survives every need to the first
        line that counts the part at all — surplus is visible, never dropped. A
        row no line counts is skipped, exactly like a ``qty_per_unit = 0`` part.
        """
        fed: set[int] = set()
        if archive.status not in (_DONE, _RUNNING):
            return fed  # neither usable nor in progress: a failed or cancelled print counts nowhere
        for row in rows:
            takers = [(ln, pf) for ln in lines if (pf := counted(ln, row.name_key)) is not None]
            if not takers:
                continue
            quantity = row_quantity(row, archive.status)
            for line, pf in takers:
                give = min(quantity, _room(pf, archive.status))
                if not give:
                    continue
                _award(pf, archive.status, give)
                quantity -= give
                fed.add(line.id)
                if not quantity:
                    break
            if quantity:
                _award(takers[0][1], archive.status, quantity)
                fed.add(takers[0][0].id)
        return fed

    def uncounted_home(rows: list[PrintArchivePart], lines: list[ProjectLine]) -> ProjectLine:
        """Which candidate lists an archive that credited nobody.

        ``hand_out`` feeds nothing when every row nets to zero (a plate scrapped
        in full) or when the print failed. The work is still the order's, so it
        is listed — and the ROWS say whose: the first candidate whose product
        counts any of the row keys. Listing the first candidate flat put a
        scrapped batch of lid_b against the product that has no lid_b on that
        bed, which reads as that line's failure.

        With nothing to read — a failed print that produced no rows at all, a
        plate of test pieces no product knows — the first candidate stands.
        """
        for line in lines:
            if any(counted(line, row.name_key) is not None for row in rows):
                return line
        return lines[0]

    def list_under(archive: PrintArchive, fed: set[int]) -> None:
        """``archive_ids`` in processing order, no duplicates. One archive may
        legitimately appear under several lines; time, grams and cost are summed
        per project, so nothing is double-counted by that."""
        for line_id, figs in figures.items():
            if line_id in fed:
                figs.archive_ids.append(archive.id)
                if archive.status == _RUNNING:
                    figs.prints_in_progress += 1

    other: list[PrintArchive] = []
    # Explicit filings first, oldest first within each group, so a hand-filed
    # print counts towards a line's need before the greedy pass deals out the
    # rest — otherwise one line absorbs every loose print while its sibling sits
    # empty.
    explicit = [a for a in ctx.archives if a.project_line_id in figures]
    implicit = [a for a in ctx.archives if a.project_line_id not in figures]
    for archive in explicit:
        home = line_by_id[archive.project_line_id]
        rows = ctx.archive_parts_by_archive.get(archive.id, [])
        home_rows = [row for row in rows if counted(home, row.name_key) is not None]
        foreign_rows = [row for row in rows if counted(home, row.name_key) is None]
        # With the home as the only taker the greedy pass fills its room and drops
        # the leftover on it as surplus — which IS "in full, need or no need": an
        # operator's filing is never second-guessed. Rows the home's product does
        # not count fall through to the other candidates rather than vanishing.
        fed = hand_out(archive, home_rows, [home]) | hand_out(archive, foreign_rows, candidates(archive, exclude=home))
        list_under(archive, fed | {home.id})
    for archive in implicit:
        lines = candidates(archive)
        if not lines:
            other.append(archive)
            continue
        # Candidates but nothing counted — a failed print, a plate of test pieces:
        # the work still belongs to the order, so it is listed uncounted on the
        # candidate its rows point at instead of being reported as a stranger's.
        rows = ctx.archive_parts_by_archive.get(archive.id, [])
        list_under(archive, hand_out(archive, rows, lines) or {uncounted_home(rows, lines).id})
    for line_id, figs in figures.items():
        figs.prints_queued = ctx.queued_by_line.get(line_id, 0)
    for figs in figures.values():
        _finish(figs)
    return figures, other


def procurement_figures(ctx: OrderContext) -> list[ProcurementFigures]:
    """Need per purchased part is Σ over the ORDER's lines — printed progress
    never enters it, which is why no line figures are taken here."""
    out: list[ProcurementFigures] = []
    for product_id, parts in ctx.parts_by_product.items():
        ordered = sum(line.quantity for line in ctx.lines if line.product_id == product_id)
        for part in parts:
            if part.kind != "purchased" or part.qty_per_unit <= 0:
                continue
            need = part.qty_per_unit * ordered
            acquired = ctx.procurement_by_part.get(part.id, 0)
            out.append(
                ProcurementFigures(
                    part_id=part.id, name=part.name, need=need, acquired=acquired, remaining=max(0, need - acquired)
                )
            )
    return out


def _units_complete(ctx: OrderContext, product_id: int, printed: int) -> int:
    kits = printed
    for part in ctx.parts_by_product.get(product_id, []):
        if part.kind == "purchased" and part.qty_per_unit > 0:
            kits = min(kits, ctx.procurement_by_part.get(part.id, 0) // part.qty_per_unit)
    return kits


def project_figures(
    ctx: OrderContext, line_figures: dict[int, LineFigures], other: list[PrintArchive]
) -> ProjectFigures:
    pf = ProjectFigures()
    printed_by_product: dict[int, int] = defaultdict(int)
    for figs in line_figures.values():
        pf.ordered += figs.quantity
        pf.printed += figs.units_printed
        # Kits off the shelf are units the order HAS, so they enter ``complete``
        # (still gated by the purchased parts — a kit with no screws assembles
        # into nothing) and every "still needed" figure below. Only ``printed``
        # and ``ordered`` stay literal: the customer ordered that many and the
        # farm printed this many.
        #
        # ⚠️ CAPPED AT THE LINE before it is summed, unlike the per-line figure
        # which stays the raw ledger reading. A line ordering ONE while holding
        # five kits (the shelf was taken before the quantity was edited down)
        # would otherwise donate its four spare kits to a sibling line that has
        # nothing, and the order would report five units complete out of two
        # ordered. Per line the honest number matters; summed, only what THIS
        # line can use does.
        from_stock = min(figs.from_stock_units, figs.quantity)
        pf.from_stock_units += from_stock
        printed_by_product[figs.product_id] += figs.units_printed + from_stock
        # NOT capped, and nothing like ``from_stock_units``: this is a count of
        # PARTS the button would move, summed exactly as ``bank_surplus`` writes
        # them (Ruling 30). The two must be the same arithmetic or the button
        # lights over an order it then reports "nothing to bank" for.
        pf.bankable_surplus += sum(p.bankable for p in figs.parts)
    pf.complete = sum(_units_complete(ctx, pid, printed) for pid, printed in printed_by_product.items())
    pf.remaining = max(0, pf.ordered - pf.printed - pf.from_stock_units)
    for a in ctx.archives:
        pf.total_time_seconds += int(a.actual_time_seconds or a.print_time_seconds or 0)
        pf.total_filament_grams += float(a.filament_used_grams or 0)
        pf.total_cost += float(a.cost or 0) + float(a.energy_cost or 0)
        pf.defective += int(a.defective_count or 0)
    pf.total_filament_grams = round(pf.total_filament_grams, 2)
    pf.total_cost = round(pf.total_cost, 2)
    pf.margin = round(ctx.project.price - pf.total_cost, 2) if ctx.project.price is not None else None
    pf.prints_in_progress = sum(1 for a in ctx.archives if a.status == _RUNNING)
    pf.prints_queued = sum(ctx.queued_by_line.values()) + ctx.queued_unfiled
    # Capped for the same reason a line's is (see ``_finish``): ``printed`` and
    # ``ordered`` sit beside it uncapped, so an overprinted order still reads
    # "5 of 3" while its bar stays full rather than overflowing its track.
    pf.progress = min(1.0, round((pf.printed + pf.from_stock_units) / pf.ordered, 4)) if pf.ordered else 0.0
    pf.other_prints_count = len(other)
    # ⚠️ ``all_printed`` is what the close-the-order banner reads, so a line
    # covered entirely from stock must satisfy it — otherwise an order that
    # needs no further print never suggests closing.
    pf.all_printed = bool(line_figures) and all(
        f.units_printed + f.from_stock_units >= f.quantity for f in line_figures.values()
    )
    return pf


# ---------- the same figures, for many orders at once ----------


@dataclass(slots=True)
class GroupedLineFigures:
    """One order line's units, as ``attribute`` counts them.

    ``usable_units`` is ``LineFigures.units_printed`` — the number of whole units
    the line's parts support, and deliberately NOT capped: an overprinted line
    reports 3 against an ordered 2, which is what the product page and the order
    page both show. ``need`` is the line's own quantity, so a caller that wants
    the capped number takes ``min`` of the two and every caller that does not
    keeps the honest one.

    ⚠️ No ``project_id`` here and no pre-capped ``printed_units``: both were
    carried for a reader that never appeared, and the order id is already on the
    :class:`GroupedOrderFigures` the line hangs off. A figure nobody reads is a
    figure nobody notices going wrong.
    """

    line_id: int
    product_id: int
    need: int
    usable_units: int
    #: Kits this line took off the product's free stock (pass 8, Decision 5).
    #: Beside ``usable_units``, never inside it: one is prints, the other is
    #: the shelf, and the caller that wants "done" adds them.
    from_stock_units: int = 0


@dataclass(slots=True)
class GroupedOrderFigures:
    """The order-level half. ``total_cost`` lives HERE and not on the line: an
    archive may legitimately be listed under several lines (a shared plate), so
    the cost is summed once per order over its archives — putting it on the line
    would double-count it the moment anybody added the lines up."""

    project_id: int
    ordered: int
    printed: int
    progress: float
    total_cost: float
    #: The order's kits off the shelf, ALREADY CAPPED per line — literally
    #: ``ProjectFigures.from_stock_units``, copied rather than re-summed here.
    #: The cap (``min(from_stock_units, quantity)`` before the sum) is a rule
    #: with a reason, spelled out in :func:`project_figures`; a caller adding
    #: the lines up itself would be the second place it lives, and the first
    #: one to forget it.
    from_stock_units: int = 0
    #: ``ProjectFigures.bankable_surplus``, copied for the same reason
    #: ``from_stock_units`` is: the parts «Списати надлишок» would still move
    #: (Ruling 30), computed once in :func:`project_figures` and never re-summed
    #: by a second reader.
    bankable_surplus: int = 0
    prints_in_progress: int = 0
    prints_queued: int = 0
    lines: list[GroupedLineFigures] = field(default_factory=list)


async def batch_contexts(db: AsyncSession, project_ids: Sequence[int]) -> list[OrderContext]:
    """Every :class:`OrderContext` in ``project_ids``, in a fixed number of queries.

    The per-order loader is right for one order and wrong once per row of a
    list — five statements per order plus every archive of it, which is how the
    orders list, the customer page and every product endpoint each ran an N+1.

    This is the SAME loader, batched: identical filters, identical ordering,
    identical indexes, so the contexts it returns are the ones
    :func:`load_order_context` would have returned one at a time — and a test
    asserts exactly that. The arithmetic afterwards is untouched; nothing here
    re-derives a figure in SQL, because a second implementation of the
    attribution rules is a second answer waiting to disagree with the first.

    "Fixed" is NINE statements — projects, their lines, products, their parts,
    their plates, archives, archive parts, procurement, stock reservations — up
    to three chunkings: ``selectin``'s own 500-parent split on each of the
    three eager loads, the 500-id slicing of the archive-parts ``IN`` below,
    and the reservation reader's own. So it is fixed in the number of ORDERS
    and not quite constant in the size of the farm; nothing here degrades to
    per-order.

    Public since pass 8: ``plan_engine`` has imported it since pass 7 (the
    candidates endpoint plans a page of orders at once), and a leading
    underscore on a name two modules already share only tells the next reader
    something untrue.
    """
    if not project_ids:
        return []
    projects = (
        (await db.execute(select(Project).options(selectinload(Project.lines)).where(Project.id.in_(project_ids))))
        .scalars()
        .all()
    )
    lines_by_project = {p.id: sorted(p.lines, key=lambda line: (line.sort_order, line.id)) for p in projects}
    product_ids = {line.product_id for lines in lines_by_project.values() for line in lines}
    products = (
        (
            await db.execute(
                select(Product)
                .options(selectinload(Product.parts), selectinload(Product.plates))
                .where(Product.id.in_(product_ids))
            )
        )
        .scalars()
        .all()
        if product_ids
        else []
    )
    products_by_id = {p.id: p for p in products}
    archives_by_project: dict[int, list[PrintArchive]] = defaultdict(list)
    for archive in (
        (
            await db.execute(
                select(PrintArchive)
                .where(PrintArchive.project_id.in_(project_ids), PrintArchive.deleted_at.is_(None))
                .order_by(PrintArchive.project_id, PrintArchive.created_at, PrintArchive.id)
            )
        )
        .scalars()
        .all()
    ):
        archives_by_project[archive.project_id].append(archive)
    archive_ids = [a.id for archives in archives_by_project.values() for a in archives]
    parts_by_archive: dict[int, list[PrintArchivePart]] = defaultdict(list)
    # ⚠️ Chunked, and it is the only list here that is. The three project-keyed
    # ``IN``s above grow with the NUMBER OF ORDERS the caller asked about —
    # unpaginated today (``list_projects`` answers every matching order), but
    # bounded by a farm's order count. This one multiplies that by the prints
    # each order carries: a few hundred orders of a few hundred prints each puts
    # tens of thousands of bound parameters into one statement, and SQLite
    # refuses past 32766 of them (``too many SQL variables``); PostgreSQL accepts
    # it and plans it badly. Slicing costs one extra statement per 500 archives
    # and bounds the worst case instead.
    for start in range(0, len(archive_ids), IN_CHUNK):
        for row in (
            (
                await db.execute(
                    select(PrintArchivePart)
                    .where(PrintArchivePart.archive_id.in_(archive_ids[start : start + IN_CHUNK]))
                    # The per-order loader's ordering, for the reason stated
                    # there: ``hand_out`` reads these in sequence. Each slice is
                    # ordered and the rows are bucketed per archive below, so the
                    # concatenation of the slices cannot disturb a single
                    # archive's own sequence.
                    .order_by(PrintArchivePart.id)
                )
            )
            .scalars()
            .all()
        ):
            parts_by_archive[row.archive_id].append(row)
    procurement_by_project: dict[int, dict[int, int]] = defaultdict(dict)
    for row in (
        await db.execute(select(ProjectProcurement).where(ProjectProcurement.project_id.in_(project_ids)))
    ).scalars():
        procurement_by_project[row.project_id][row.product_part_id] = row.quantity_acquired
    # One ledger read for every line of every order asked about — the same
    # helper the per-order loader uses, so the two cannot drift about what a
    # line has taken off the shelf or already banked onto it.
    reads = await _load_reserved(
        db,
        [line.id for lines in lines_by_project.values() for line in lines],
        _counted_qty_per_unit(products),
    )
    reserved = reads.reserved_units
    # Same helper the per-order loader uses, so a list row and the page it opens
    # cannot disagree about what is waiting in either queue tier.
    queued_all = await _load_queued(db, list(project_ids))

    out: list[OrderContext] = []
    for project in projects:
        lines = lines_by_project[project.id]
        queued = queued_all.get(project.id, {})
        # Built from THIS order's products only, exactly as the per-order loader
        # does. A shared index over every order's products would answer the same
        # (``candidates`` filters on the order's own lines) but it would be a
        # different object than the one parity is claimed against.
        own_products = [products_by_id[pid] for pid in {line.product_id for line in lines} if pid in products_by_id]
        plate_products, whole_file_products = index_plates(
            plate for product in own_products for plate in product.plates
        )
        archives = archives_by_project.get(project.id, [])
        line_ids = {line.id for line in lines}
        out.append(
            OrderContext(
                project=project,
                lines=lines,
                products_by_id={p.id: p for p in own_products},
                parts_by_product={p.id: list(p.parts) for p in own_products},
                plate_product=plate_products,
                archives=list(archives),
                archive_parts_by_archive={a.id: parts_by_archive[a.id] for a in archives if a.id in parts_by_archive},
                procurement_by_part=procurement_by_project.get(project.id, {}),
                whole_file_product=whole_file_products,
                reserved_by_line={line.id: reserved[line.id] for line in lines if line.id in reserved},
                banked_by_line_part={key: net for key, net in reads.banked_by_part.items() if key[0] in line_ids},
                queued_by_line={lid: n for lid, n in queued.items() if lid is not None},
                queued_unfiled=queued.get(None, 0),
            )
        )
    return out


async def grouped_figures(
    db: AsyncSession, *, project_ids: Sequence[int] | None = None, product_ids: Sequence[int] | None = None
) -> list[GroupedOrderFigures]:
    """The order figures for many orders in one round of queries.

    Ask by order (the orders list, the customer page) or by product (the product
    page, which knows a product and not the orders it sits in — ``product_ids``
    resolves them through ``project_lines``, and finds cancelled orders too:
    "how many were ever printed" counts every status).

    Cost of an empty ask is nothing; asking for neither is a programming error,
    not "everything", because a silent full-table sweep is what this function
    exists to stop.
    """
    if project_ids is None and product_ids is None:
        raise ValueError("grouped_figures needs project_ids or product_ids")
    ids = set(project_ids or ())
    if product_ids:
        ids |= set(
            (await db.execute(select(ProjectLine.project_id).where(ProjectLine.product_id.in_(product_ids)).distinct()))
            .scalars()
            .all()
        )
    out: list[GroupedOrderFigures] = []
    for ctx in await batch_contexts(db, sorted(ids)):
        line_figures, other = attribute(ctx)
        pf = project_figures(ctx, line_figures, other)
        out.append(
            GroupedOrderFigures(
                project_id=ctx.project.id,
                ordered=pf.ordered,
                printed=pf.printed,
                progress=pf.progress,
                total_cost=pf.total_cost,
                from_stock_units=pf.from_stock_units,
                bankable_surplus=pf.bankable_surplus,
                prints_in_progress=pf.prints_in_progress,
                prints_queued=pf.prints_queued,
                lines=[
                    GroupedLineFigures(
                        line_id=figs.line_id,
                        product_id=figs.product_id,
                        need=figs.quantity,
                        usable_units=figs.units_printed,
                        from_stock_units=figs.from_stock_units,
                    )
                    for figs in (line_figures[line.id] for line in ctx.lines)
                ],
            )
        )
    return out


def units_delivered(figures: Iterable[GroupedOrderFigures], product_id: int) -> int:
    """How many units of ``product_id`` the given orders have printed, all statuses.

    ``usable_units`` is NOT capped at the line's ``need`` — a surplus is awarded
    to the first taker and stays on its line, so an order that printed 3 of an
    ordered 2 reports 3 here, exactly as the order page reports it. Capping only
    this side would make the product page and the order page disagree about the
    same prints, which is the one thing this helper exists to prevent.

    (The uncapped reading is also the one :class:`GroupedLineFigures` documents.
    This docstring used to claim the opposite — that ``need`` was "already
    inside" the number — which is what a reader would have believed while
    watching a product page under-report every overprinted order.)
    """
    return sum(line.usable_units for order in figures for line in order.lines if line.product_id == product_id)


async def customer_figures(db: AsyncSession, customer_id: int) -> dict:
    rows = (
        await db.execute(select(Project.id, Project.status, Project.price).where(Project.customer_id == customer_id))
    ).all()
    out = {
        "projects": len(rows),
        "active": 0,
        "completed": 0,
        "cancelled": 0,
        "ordered": 0,
        "printed": 0,
        "total_cost": 0.0,
        "total_price": 0.0,
    }
    for _project_id, status, price in rows:
        # A status this build has never heard of is counted under its own key
        # rather than dropped — the same rule the list endpoint follows.
        out[status] = out.get(status, 0) + 1
        # ⚠️ The price is summed UNCONDITIONALLY now. It used to be added after
        # a per-project context load, past a ``continue`` that skipped an order
        # which had vanished between the two reads — so such an order was
        # counted under its status and then never priced. An ordering quirk of
        # the old loop, unreachable now that both facts come off one snapshot.
        out["total_price"] += float(price or 0)
    for order in await grouped_figures(db, project_ids=[project_id for project_id, _s, _p in rows]):
        out["ordered"] += order.ordered
        out["printed"] += order.printed
        out["total_cost"] += order.total_cost
    out["total_cost"] = round(out["total_cost"], 2)
    out["total_price"] = round(out["total_price"], 2)
    return out


__all__ = [
    "IN_CHUNK",
    "GroupedLineFigures",
    "GroupedOrderFigures",
    "LineFigures",
    "OrderContext",
    "PartFigures",
    "ProcurementFigures",
    "ProjectFigures",
    "archive_material_set",
    "attribute",
    "batch_contexts",
    "customer_figures",
    "grouped_figures",
    "line_accepts_materials",
    "load_order_context",
    "procurement_figures",
    "project_figures",
    "units_delivered",
]
