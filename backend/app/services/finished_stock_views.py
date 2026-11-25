"""Finished-goods positions on the wire — read-only (spec workshop-finished-goods, rules 16–19).

A page of positions is built with a fixed number of statements whatever its
size: the products, their groups, their parts, the positions' configurations,
the standard options and ONE read of the free-parts shelf for every product on
the page (``part_stock.balances_for_products``) — ``can_assemble`` is the
kit count of each position's own configuration.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.finished_stock import StockItem, StockItemMovement
from backend.app.models.product import Product, ProductPart
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.schemas.finished_stock import (
    StockItemDetail,
    StockItemOut,
    StockItemPartOut,
    StockItemSibling,
    StockProductRef,
    StockReservationOut,
)
from backend.app.services import part_stock
from backend.app.services.configuration_views import configuration_out, groups_by_product
from backend.app.services.entity_codes import code_for
from backend.app.services.line_composition import (
    LineConfig,
    counted,
    default_options,
    load_item_configs,
    stock_composition,
)
from backend.app.services.product_files import effective_cover

#: WS-13 E1 PC1/PC3 — the ONE definition of «below the minimum» and of a «tracked»
#: position, as SQL, for every reader: the stock page and its sidebar count, the
#: catalog's filter and row counts, the menu badge. ``below_min`` below is its row twin.
BELOW_MIN = and_(StockItem.min_qty > 0, StockItem.on_hand - StockItem.reserved < StockItem.min_qty)
TRACKED = or_(StockItem.on_hand > 0, StockItem.reserved > 0, StockItem.min_qty > 0)


def below_min(item: StockItem) -> bool:
    return item.min_qty > 0 and item.on_hand - item.reserved < item.min_qty


def short_by(item: StockItem) -> int:
    return item.min_qty - (item.on_hand - item.reserved) if below_min(item) else 0


def parts_out(kit, shelf) -> list[StockItemPartOut]:
    """The kit's printed parts with what one unit takes and what the free shelf holds."""
    return [
        StockItemPartOut(part_id=part.id, name=part.name, per=per, on_shelf=shelf.get(part.id, 0))
        for part, per in counted(kit)
    ]


async def _parts_by_product(db: AsyncSession, product_ids: Sequence[int]) -> dict[int, list[ProductPart]]:
    out: dict[int, list[ProductPart]] = {pid: [] for pid in product_ids}
    if not product_ids:
        return out
    for part in (await db.execute(select(ProductPart).where(ProductPart.product_id.in_(product_ids)))).scalars():
        out.setdefault(part.product_id, []).append(part)
    return out


class _Context:
    """Everything a page of positions reads, loaded once."""

    def __init__(self, products, groups, parts, configs, defaults, balances):
        self.products = products
        self.groups = groups
        self.parts = parts
        self.configs = configs
        self.defaults = defaults
        self.balances = balances

    def configuration(self, item: StockItem):
        return configuration_out(
            self.groups.get(item.product_id, []),
            self.parts.get(item.product_id, []),
            self.configs.get(item.id, LineConfig()),
            self.defaults.get(item.product_id, {}),
        )

    def composition(self, item: StockItem):
        return stock_composition(
            item,
            self.parts.get(item.product_id, []),
            self.configs.get(item.id, LineConfig()),
            self.defaults.get(item.product_id, {}),
        )

    def can_assemble(self, item: StockItem) -> int:
        return part_stock.kits_of(self.balances.get(item.product_id, {}), self.composition(item))

    def out(self, item: StockItem) -> StockItemOut:
        product = self.products[item.product_id]
        return StockItemOut(
            id=item.id,
            code=code_for("stock_item", item.id),
            product=StockProductRef(
                id=product.id, name=product.name, sku=product.sku, has_cover=effective_cover(product) is not None
            ),
            configuration=self.configuration(item),
            location=item.location,
            on_hand=item.on_hand,
            reserved=item.reserved,
            available=item.on_hand - item.reserved,
            min_qty=item.min_qty,
            below_min=below_min(item),
            short_by=short_by(item),
            can_assemble=self.can_assemble(item),
        )


async def _context(db: AsyncSession, items: Sequence[StockItem], *, with_balances: bool = True) -> _Context:
    product_ids = sorted({item.product_id for item in items})
    products = (
        {p.id: p for p in (await db.execute(select(Product).where(Product.id.in_(product_ids)))).scalars()}
        if product_ids
        else {}
    )
    balances = await part_stock.balances_for_products(db, product_ids) if (product_ids and with_balances) else {}
    return _Context(
        products,
        await groups_by_product(db, product_ids),
        await _parts_by_product(db, product_ids),
        await load_item_configs(db, [item.id for item in items]),
        await default_options(db, product_ids),
        balances,
    )


async def items_out(db: AsyncSession, items: Sequence[StockItem]) -> list[StockItemOut]:
    """A page of positions — a fixed number of statements, one shelf read."""
    if not items:
        return []
    ctx = await _context(db, items)
    return [ctx.out(item) for item in items]


async def item_detail(db: AsyncSession, item: StockItem) -> StockItemDetail:
    """The position page: its row, reservation groups, sibling configurations
    and the free parts under its kit (spec rule 18)."""
    siblings = (
        (
            await db.execute(
                select(StockItem)
                .where(StockItem.product_id == item.product_id, StockItem.id != item.id)
                .order_by(StockItem.id)
            )
        )
        .scalars()
        .all()
    )
    ctx = await _context(db, [item, *siblings])
    groups = (
        await db.execute(
            select(StockItemMovement.project_line_id, func.sum(StockItemMovement.delta_reserved))
            .where(StockItemMovement.item_id == item.id)
            .group_by(StockItemMovement.project_line_id)
        )
    ).all()
    line_ids = [line_id for line_id, qty in groups if line_id is not None and qty]
    # The order of each line, with its name for the position page (WS-13 E12 F6, A02); an order
    # that is gone leaves the line its id and no name.
    orders = (
        {
            line_id: (project_id, name)
            for line_id, project_id, name in (
                await db.execute(
                    select(ProjectLine.id, ProjectLine.project_id, Project.name)
                    .outerjoin(Project, Project.id == ProjectLine.project_id)
                    .where(ProjectLine.id.in_(line_ids))
                )
            ).all()
        }
        if line_ids
        else {}
    )
    reservations = [
        StockReservationOut(
            project_line_id=line_id,
            project_id=orders[line_id][0] if line_id in orders else None,
            project_code=code_for("order", orders[line_id][0]) if line_id in orders else None,
            project_name=orders[line_id][1] if line_id in orders else None,
            qty=int(qty),
        )
        for line_id, qty in sorted(groups, key=lambda g: (g[0] is not None, g[0] or 0))
        if qty
    ]
    parts = parts_out(ctx.composition(item), ctx.balances.get(item.product_id, {}))
    return StockItemDetail(
        **ctx.out(item).model_dump(),
        reservations=reservations,
        siblings=[
            StockItemSibling(
                id=s.id,
                code=code_for("stock_item", s.id),
                configuration=ctx.configuration(s),
                on_hand=s.on_hand,
                available=s.on_hand - s.reserved,
            )
            for s in siblings
        ],
        parts=parts,
    )


async def configuration_refs(db: AsyncSession, items: Sequence[StockItem]) -> dict[int, tuple[str, object]]:
    """``item_id → (code, configuration)`` for the journal — no shelf read."""
    if not items:
        return {}
    ctx = await _context(db, items, with_balances=False)
    return {item.id: (code_for("stock_item", item.id), ctx.configuration(item)) for item in items}
