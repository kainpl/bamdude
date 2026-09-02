"""Archive → order-line attribution and every order figure (spec §Fact attribution).

The archive is the only source of fact. Queue tables are never read here.
Everything is computed on read over one loaded ``OrderContext``; an order has
tens to hundreds of archives, which is nothing.
"""

from __future__ import annotations

from collections import defaultdict
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
    plate_product: dict[tuple[int, int], int]  # (library_file_id, plate_index) → product_id
    archives: list[PrintArchive]  # not trashed, oldest first
    archive_parts_by_archive: dict[int, list[PrintArchivePart]]
    procurement_by_part: dict[int, int]  # product_part_id → quantity_acquired


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
    plate_product: dict[tuple[int, int], int] = {}
    for product in products:
        for plate in product.plates:
            plate_product[(plate.library_file_id, plate.plate_index)] = product.id
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
        (await db.execute(select(PrintArchivePart).where(PrintArchivePart.archive_id.in_([a.id for a in archives]))))
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
        plate_product=plate_product,
        archives=list(archives),
        archive_parts_by_archive=dict(by_archive),
        procurement_by_part=procurement,
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
    figs.progress = round(figs.units_printed / figs.quantity, 4) if figs.quantity else 0.0


def _apply(figs: LineFigures, archive: PrintArchive, rows: list[PrintArchivePart], idx: dict[str, ProductPart]) -> None:
    by_id = {p.part_id: p for p in figs.parts}
    for row in rows:
        part = idx.get(row.name_key)
        if part is None or part.id not in by_id:
            continue
        target = by_id[part.id]
        if archive.status == _DONE:
            target.usable += max(0, (row.quantity or 0) - (row.defective or 0))
        elif archive.status == _RUNNING:
            target.in_progress += row.quantity or 0
    figs.archive_ids.append(archive.id)


def _line_accepts(line: ProjectLine, materials: set[str]) -> bool:
    return line.material is None or line.material.strip().upper() in materials


def attribute(ctx: OrderContext) -> tuple[dict[int, LineFigures], list[PrintArchive]]:
    figures = {line.id: _new_line_figures(line, ctx.parts_by_product.get(line.product_id, [])) for line in ctx.lines}
    indexes = {pid: part_index(parts) for pid, parts in ctx.parts_by_product.items()}
    lines_by_product: dict[int, list[ProjectLine]] = defaultdict(list)
    for line in ctx.lines:
        lines_by_product[line.product_id].append(line)
    other: list[PrintArchive] = []

    explicit = [a for a in ctx.archives if a.project_line_id in figures]
    implicit = [a for a in ctx.archives if a.project_line_id not in figures]
    for archive in explicit:
        figs = figures[archive.project_line_id]
        _apply(figs, archive, ctx.archive_parts_by_archive.get(archive.id, []), indexes.get(figs.product_id, {}))
    for archive in implicit:
        product_id = ctx.plate_product.get((archive.library_file_id, archive.plate_index or 0))
        materials = archive_material_set(archive.filament_type)
        candidates = [ln for ln in lines_by_product.get(product_id, []) if _line_accepts(ln, materials)]
        if product_id is None or not candidates:
            other.append(archive)
            continue
        # Sequential greedy (spec §Line resolution for an archive, step 2): the
        # first line in sort order whose need is not yet met, else the first
        # matching line. Explicit filings are applied FIRST and count towards
        # that — a line an operator already filled by hand must not keep
        # absorbing loose prints while its sibling sits empty; and once every
        # candidate is met the surplus falls back to the first matching line.
        unmet = [ln for ln in candidates if _units_printed(figures[ln.id]) < ln.quantity]
        chosen = (unmet or candidates)[0]
        _apply(
            figures[chosen.id], archive, ctx.archive_parts_by_archive.get(archive.id, []), indexes.get(product_id, {})
        )
    for figs in figures.values():
        _finish(figs)
    return figures, other


def procurement_figures(ctx: OrderContext, line_figures: dict[int, LineFigures]) -> list[ProcurementFigures]:
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
    pf.progress = round(pf.printed / pf.ordered, 4) if pf.ordered else 0.0
    pf.other_prints_count = len(other)
    pf.all_printed = bool(line_figures) and all(f.units_printed >= f.quantity for f in line_figures.values())
    return pf


async def customer_figures(db: AsyncSession, customer_id: int) -> dict:
    projects = (await db.execute(select(Project).where(Project.customer_id == customer_id))).scalars().all()
    out = {
        "projects": len(projects),
        "active": 0,
        "completed": 0,
        "cancelled": 0,
        "ordered": 0,
        "printed": 0,
        "total_cost": 0.0,
        "total_price": 0.0,
    }
    for project in projects:
        out[project.status] = out.get(project.status, 0) + 1
        ctx = await load_order_context(db, project.id)
        if ctx is None:
            continue
        figs, other = attribute(ctx)
        pf = project_figures(ctx, figs, other)
        out["ordered"] += pf.ordered
        out["printed"] += pf.printed
        out["total_cost"] += pf.total_cost
        out["total_price"] += float(project.price or 0)
    out["total_cost"] = round(out["total_cost"], 2)
    out["total_price"] = round(out["total_price"], 2)
    return out


__all__ = [
    "LineFigures",
    "OrderContext",
    "PartFigures",
    "ProcurementFigures",
    "ProjectFigures",
    "archive_material_set",
    "attribute",
    "customer_figures",
    "load_order_context",
    "procurement_figures",
    "project_figures",
]
