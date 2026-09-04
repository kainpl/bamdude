"""Archive → order-line attribution and every order figure (spec §Fact attribution).

The archive is the only source of fact. Queue tables are never read here.
Everything is computed on read over one loaded ``OrderContext``; an order has
tens to hundreds of archives, which is nothing.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.product import Product, ProductPart
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine, ProjectProcurement
from backend.app.services.product_composition import part_index

_DONE = "completed"
_RUNNING = "printing"


def archive_material_set(filament_type: str | None) -> set[str]:
    """``PrintArchive.filament_type`` is a joined string ("PLA, PETG")."""
    return {tok.strip().upper() for tok in (filament_type or "").split(",") if tok.strip()}


@dataclass
class PartFigures:
    part_id: int
    name: str
    kind: str
    qty_per_unit: int
    need: int = 0
    usable: int = 0
    in_progress: int = 0
    remaining: int = 0
    surplus: int = 0


@dataclass
class LineFigures:
    line_id: int
    product_id: int
    quantity: int
    material: str | None
    units_printed: int = 0
    progress: float = 0.0
    parts: list[PartFigures] = field(default_factory=list)
    archive_ids: list[int] = field(default_factory=list)


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
    plate_products: dict[tuple[int, int], set[int]] = defaultdict(set)
    whole_file_products: dict[int, set[int]] = defaultdict(set)
    for product in products:
        for plate in product.plates:
            plate_products[(plate.library_file_id, plate.plate_index)].add(product.id)
            if plate.plate_index == 0:
                whole_file_products[plate.library_file_id].add(product.id)
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
    return OrderContext(
        project=project,
        lines=lines,
        products_by_id={p.id: p for p in products},
        parts_by_product={p.id: list(p.parts) for p in products},
        plate_product={key: sorted(pids) for key, pids in plate_products.items()},
        archives=list(archives),
        archive_parts_by_archive=dict(by_archive),
        procurement_by_part=procurement,
        whole_file_product={file_id: sorted(pids) for file_id, pids in whole_file_products.items()},
    )


def _new_line_figures(line: ProjectLine, parts: list[ProductPart]) -> LineFigures:
    figs = LineFigures(line_id=line.id, product_id=line.product_id, quantity=line.quantity, material=line.material)
    for part in parts:
        if part.kind != "printed" or part.qty_per_unit <= 0:
            continue
        figs.parts.append(
            PartFigures(
                part_id=part.id,
                name=part.name,
                kind=part.kind,
                qty_per_unit=part.qty_per_unit,
                need=part.qty_per_unit * line.quantity,
            )
        )
    return figs


def _units_printed(figs: LineFigures) -> int:
    if not figs.parts:
        return 0
    return min(p.usable // p.qty_per_unit for p in figs.parts)


def _finish(figs: LineFigures) -> None:
    for p in figs.parts:
        p.remaining = max(0, p.need - p.usable)
        p.surplus = max(0, p.usable - p.need)
    figs.units_printed = _units_printed(figs)
    # Capped on the wire: ``progress`` is what a bar fills from, and a bar
    # cannot be 300% full. The excess is not lost — ``units_printed`` and each
    # part's ``surplus`` carry it uncapped, which is where the overprint is
    # meant to be read. Clamping at every consumer instead was the alternative,
    # and the frontend was the only one that remembered.
    figs.progress = min(1.0, round(figs.units_printed / figs.quantity, 4)) if figs.quantity else 0.0


def _line_accepts(line: ProjectLine, materials: set[str]) -> bool:
    return line.material is None or line.material.strip().upper() in materials


def _row_quantity(row: PrintArchivePart, status: str) -> int:
    """A completed row hands over what came out good; a running one its full count."""
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
    figures = {line.id: _new_line_figures(line, ctx.parts_by_product.get(line.product_id, [])) for line in ctx.lines}
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

        The exact plate first, then the whole-file wildcard: a product plate with
        ``plate_index = 0`` claims EVERY plate of that file, which is how a
        single-plate file (one 0-row from the sync) meets its prints, which carry
        the slicer's index, 1. Without the second lookup those two numbers never
        meet and every such print lands in "other".
        """
        product_ids = (
            ctx.plate_product.get((archive.library_file_id, archive.plate_index or 0))
            or ctx.whole_file_product.get(archive.library_file_id)
            or []
        )
        materials = archive_material_set(archive.filament_type)
        return [
            ln
            for ln in ctx.lines
            if ln.product_id in product_ids and ln is not exclude and _line_accepts(ln, materials)
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
            quantity = _row_quantity(row, archive.status)
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
        printed_by_product[figs.product_id] += figs.units_printed
    pf.complete = sum(_units_complete(ctx, pid, printed) for pid, printed in printed_by_product.items())
    pf.remaining = max(0, pf.ordered - pf.printed)
    for a in ctx.archives:
        pf.total_time_seconds += int(a.actual_time_seconds or a.print_time_seconds or 0)
        pf.total_filament_grams += float(a.filament_used_grams or 0)
        pf.total_cost += float(a.cost or 0) + float(a.energy_cost or 0)
        pf.defective += int(a.defective_count or 0)
    pf.total_filament_grams = round(pf.total_filament_grams, 2)
    pf.total_cost = round(pf.total_cost, 2)
    pf.margin = round(ctx.project.price - pf.total_cost, 2) if ctx.project.price is not None else None
    # Capped for the same reason a line's is (see ``_finish``): ``printed`` and
    # ``ordered`` sit beside it uncapped, so an overprinted order still reads
    # "5 of 3" while its bar stays full rather than overflowing its track.
    pf.progress = min(1.0, round(pf.printed / pf.ordered, 4)) if pf.ordered else 0.0
    pf.other_prints_count = len(other)
    pf.all_printed = bool(line_figures) and all(f.units_printed >= f.quantity for f in line_figures.values())
    return pf


# ---------- the same figures, for many orders at once ----------


@dataclass(slots=True)
class GroupedLineFigures:
    """One order line's units, as ``attribute`` counts them.

    ``usable_units`` is ``LineFigures.units_printed`` — the number of whole units
    the line's parts support, and deliberately NOT capped: an overprinted line
    reports 3 against an ordered 2, which is what the product page and the order
    page both show. ``printed_units`` is that number capped at ``need``, which is
    what a progress bar fills from.
    """

    line_id: int
    project_id: int
    product_id: int
    need: int
    usable_units: int
    printed_units: int


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
    lines: list[GroupedLineFigures] = field(default_factory=list)


async def _batch_contexts(db: AsyncSession, project_ids: Sequence[int]) -> list[OrderContext]:
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

    "Fixed" is EIGHT statements — projects, their lines, products, their parts,
    their plates, archives, archive parts, procurement — up to ``selectin``'s
    500-parent chunking, which splits each of the three eager loads once per
    further 500 parents. So it is fixed in the number of ORDERS and not quite
    constant in the size of the farm; nothing here degrades to per-order.
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
    if archive_ids:
        for row in (
            (
                await db.execute(
                    select(PrintArchivePart)
                    .where(PrintArchivePart.archive_id.in_(archive_ids))
                    # The per-order loader's ordering, for the reason stated
                    # there: ``hand_out`` reads these in sequence.
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

    out: list[OrderContext] = []
    for project in projects:
        lines = lines_by_project[project.id]
        # Built from THIS order's products only, exactly as the per-order loader
        # does. A shared index over every order's products would answer the same
        # (``candidates`` filters on the order's own lines) but it would be a
        # different object than the one parity is claimed against.
        own_products = [products_by_id[pid] for pid in {line.product_id for line in lines} if pid in products_by_id]
        plate_products: dict[tuple[int, int], set[int]] = defaultdict(set)
        whole_file_products: dict[int, set[int]] = defaultdict(set)
        for product in own_products:
            for plate in product.plates:
                plate_products[(plate.library_file_id, plate.plate_index)].add(product.id)
                if plate.plate_index == 0:
                    whole_file_products[plate.library_file_id].add(product.id)
        archives = archives_by_project.get(project.id, [])
        out.append(
            OrderContext(
                project=project,
                lines=lines,
                products_by_id={p.id: p for p in own_products},
                parts_by_product={p.id: list(p.parts) for p in own_products},
                plate_product={key: sorted(pids) for key, pids in plate_products.items()},
                archives=list(archives),
                archive_parts_by_archive={a.id: parts_by_archive[a.id] for a in archives if a.id in parts_by_archive},
                procurement_by_part=procurement_by_project.get(project.id, {}),
                whole_file_product={file_id: sorted(pids) for file_id, pids in whole_file_products.items()},
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
    for ctx in await _batch_contexts(db, sorted(ids)):
        line_figures, other = attribute(ctx)
        pf = project_figures(ctx, line_figures, other)
        out.append(
            GroupedOrderFigures(
                project_id=ctx.project.id,
                ordered=pf.ordered,
                printed=pf.printed,
                progress=pf.progress,
                total_cost=pf.total_cost,
                lines=[
                    GroupedLineFigures(
                        line_id=figs.line_id,
                        project_id=ctx.project.id,
                        product_id=figs.product_id,
                        need=figs.quantity,
                        usable_units=figs.units_printed,
                        printed_units=min(figs.units_printed, figs.quantity),
                    )
                    for figs in (line_figures[line.id] for line in ctx.lines)
                ],
            )
        )
    return out


def units_delivered(figures: Iterable[GroupedOrderFigures], product_id: int) -> int:
    """How many units of ``product_id`` the given orders have printed, all statuses.

    The per-part ``need`` cap is already inside ``usable_units`` — it is what
    ``attribute`` awards against — but the LINE's quantity is deliberately not
    applied here: an order that printed 3 of an ordered 2 really did print 3, and
    the order page says so. Capping only this side would make the product page
    and the order page disagree about the same prints, which is the one thing
    this helper exists to prevent.
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
    "GroupedLineFigures",
    "GroupedOrderFigures",
    "LineFigures",
    "OrderContext",
    "PartFigures",
    "ProcurementFigures",
    "ProjectFigures",
    "archive_material_set",
    "attribute",
    "customer_figures",
    "grouped_figures",
    "load_order_context",
    "procurement_figures",
    "project_figures",
    "units_delivered",
]
