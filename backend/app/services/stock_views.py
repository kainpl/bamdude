"""Read-side helpers shared by every route that shows the stock ledger.

Two routes render a ledger row — the product page's shelf and the farm-wide
Stock tab — and they must render it the SAME way: the order a reservation
belongs to is resolved here, once per page, and the note stays the token the
writer wrote (the frontend translates it). Nothing here writes: the ledger's
single writer is ``services/part_stock.py``.
"""

from collections.abc import Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.product import Product, ProductPart
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.schemas.product import KitsByOptionOut, ProductPartVariantOut, StockMovementOut
from backend.app.services import part_stock
from backend.app.services.line_composition import composition


async def kits_by_option(
    db: AsyncSession, products: Sequence[Product], balances: Mapping[int, Mapping[int, int]]
) -> dict[int, list[KitsByOptionOut]]:
    """``product_id → the kits of each option`` (WS-13 E1 ST4 / ST5, Q12): for every
    option of every group, the kits the free shelf makes with THAT option and every other
    group at its standard — ``line_composition.composition`` + ``part_stock.kits_of``,
    the same arithmetic as the row's own ``kits_available``. One statement for all the
    products given (``parts`` loaded); a product without groups gets ``[]``. The stock
    page asks it for its page's rows only, the product page for its one product."""
    out: dict[int, list[KitsByOptionOut]] = {p.id: [] for p in products}
    if not out:
        return out
    rows = (
        await db.execute(
            select(
                ProductVariantGroup.product_id,
                ProductVariantGroup.id,
                ProductVariantGroup.name,
                ProductVariantGroup.default_option_id,
                ProductVariantOption.id,
                ProductVariantOption.name,
            )
            .join(ProductVariantOption, ProductVariantOption.group_id == ProductVariantGroup.id)
            .where(ProductVariantGroup.product_id.in_(list(out)))
            .order_by(
                ProductVariantGroup.product_id,
                ProductVariantGroup.position,
                ProductVariantGroup.id,
                ProductVariantOption.position,
                ProductVariantOption.id,
            )
        )
    ).all()
    standard: dict[int, dict[int, int]] = {}
    for product_id, group_id, _gname, default_id, _oid, _oname in rows:
        if default_id is not None:
            standard.setdefault(product_id, {})[group_id] = default_id
    by_id = {p.id: p for p in products}
    for product_id, group_id, group_name, default_id, option_id, option_name in rows:
        others = {oid for gid, oid in standard.get(product_id, {}).items() if gid != group_id}
        kit = composition(list(by_id[product_id].parts), "product", others | {option_id}, {})
        out[product_id].append(
            KitsByOptionOut(
                group_id=group_id,
                group_name=group_name,
                option_id=option_id,
                option_name=option_name,
                is_default=option_id == default_id,
                kits=part_stock.kits_of(balances.get(product_id, {}), kit),
            )
        )
    return out


async def binding_labels(db: AsyncSession, parts: Iterable[ProductPart]) -> dict[int, list[ProductPartVariantOut]]:
    """``part_id → [{group, options}]`` for the parts a page shows — one statement.

    A part bound across several groups names each group once, with the values it takes
    there (spec product-part-variant-combinations): groups in their position order, the
    values in their group's. A part with no binding is absent (the caller's ``[]``). The
    parts' ``option_links`` must be loaded (any query of ``ProductPart`` loads them)."""
    bound = {part.id: {link.option_id for link in part.option_links} for part in parts}
    option_ids = set().union(*bound.values()) if bound else set()
    if not option_ids:
        return {}
    rows = (
        await db.execute(
            select(ProductVariantOption.id, ProductVariantGroup.id, ProductVariantGroup.name, ProductVariantOption.name)
            .join(ProductVariantGroup, ProductVariantGroup.id == ProductVariantOption.group_id)
            .where(ProductVariantOption.id.in_(option_ids))
            .order_by(
                ProductVariantGroup.position,
                ProductVariantGroup.id,
                ProductVariantOption.position,
                ProductVariantOption.id,
            )
        )
    ).all()
    out: dict[int, list[ProductPartVariantOut]] = {}
    for part_id, options in bound.items():
        names: dict[tuple[int, str], list[str]] = {}  # insertion order = the groups' order
        for option_id, group_id, group_name, option_name in rows:
            if option_id in options:
                names.setdefault((group_id, group_name), []).append(option_name)
        if names:
            out[part_id] = [
                ProductPartVariantOut(group=group, options=values) for (_gid, group), values in names.items()
            ]
    return out


async def orders_of_lines(db: AsyncSession, line_ids: set[int]) -> dict[int, tuple[int, str]]:
    """``line_id → (order id, order name)`` for a whole page of movements, in ONE join.

    A reservation names an order LINE and nothing else — the ledger has no
    order column, because a line already has one and two would be able to
    disagree. The page still has to show the order the operator recognises, so
    the hop is made here, once for every line on the page rather than once per
    movement.

    A line id that resolves to nothing is simply absent: the caller reads
    ``.get(...)`` and sends ``None``. That is a deleted line whose rows
    ``part_stock.detach_line`` has not reached (PostgreSQL's ``SET NULL`` and
    SQLite's silence differ here), and a movement with no order left is still a
    movement that happened.
    """
    if not line_ids:
        return {}
    rows = await db.execute(
        select(ProjectLine.id, Project.id, Project.name)
        .join(Project, Project.id == ProjectLine.project_id)
        .where(ProjectLine.id.in_(line_ids))
    )
    return {line_id: (order_id, name) for line_id, order_id, name in rows.all()}


def movement_out(
    row: ProductPartStockMovement, names: dict[int, str], orders: dict[int, tuple[int, str]]
) -> StockMovementOut:
    """One ledger row on the wire, with its part named and its order resolved.

    Both maps are the caller's — built once for a whole page — so this stays a
    pure formatter with no query hidden in it.
    """
    order = orders.get(row.project_line_id) if row.project_line_id is not None else None
    return StockMovementOut(
        id=row.id,
        part_id=row.product_part_id,
        part_name=names[row.product_part_id],
        delta=row.delta,
        reason=row.reason,
        project_line_id=row.project_line_id,
        order_id=order[0] if order else None,
        order_name=order[1] if order else None,
        archive_id=row.archive_id,
        note=row.note,
        created_by=row.created_by,
        created_at=row.created_at,
    )
