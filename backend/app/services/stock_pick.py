"""What an order line would take from stock — ONE function (spec workshop-add-to-order, rule 5).

Ready units of the line's own configuration first (its finished-goods position),
then whole kits of free parts for that configuration, up to the quantity; the
rest is printed. The add-to-order dialog's proposal (``POST /stock/suggest``),
the batch's ``stock: "auto"`` and the order card's «take from stock» (WS-11)
all ask this function, so the three cannot disagree about what the shelf holds.

Read-only, and a fixed number of statements whatever the length of the list:
the products with their parts and groups in one read, the positions of every
(product, configuration) in one, the free-parts balances of every product in
one. A request naming an existing ``line_id`` also counts what that line already
holds as free for it — the line may keep its own reservation when it is
rewritten (the writers release before they take) — and what it already issued
(a reactivated order's line counts its shipped units as its own); the named
lines are read together, a fixed handful of statements for any number of them.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.finished_stock import StockItemMovement
from backend.app.models.product import Product, ProductOrigin
from backend.app.models.project_line import ProjectLine
from backend.app.services import finished_stock, line_config, part_stock
from backend.app.services.entity_codes import code_for
from backend.app.services.line_composition import (
    Composition,
    composition,
    compositions_for_lines,
    counted,
    snapshot_parts,
)


class StockPickError(Exception):
    """A request the proposal cannot answer; ``status`` is the HTTP answer."""

    def __init__(self, detail: str, status: int = 422) -> None:
        super().__init__(detail)
        self.status = status


@dataclass(frozen=True, slots=True)
class PickRequest:
    product_id: int
    choices: Mapping[int, int]
    counts: Mapping[int, int]
    quantity: int
    line_id: int | None = None
    saved_kit: Composition | None = None


@dataclass(frozen=True, slots=True)
class Suggestion:
    product_id: int
    #: Free ready units in the position of THIS configuration.
    finished_free: int
    #: Whole kits of this configuration the free-parts shelf can make.
    kits_free: int
    from_finished: int
    from_kits: int
    to_print: int
    position_id: int | None
    position_code: str | None


@dataclass(frozen=True, slots=True)
class _LineBack:
    """What an existing line may keep: its ready units by position, the ones it
    already issued (a reactivated order's — they stay the line's, rule 1), and
    its reserved kits with the composition they are counted in."""

    product_id: int
    held_by_item: dict[int, int]
    issued: int
    kits: int
    kit: Composition


async def _lines_back(
    db: AsyncSession, line_ids: Sequence[int], products: Mapping[int, Product]
) -> dict[int, _LineBack]:
    """Every named line's :class:`_LineBack` — a fixed number of statements for any
    number of lines (final review M1): the lines, their holdings by position, their
    compositions and their kits, each in one read."""
    if not line_ids:
        return {}
    lines = (await db.execute(select(ProjectLine).where(ProjectLine.id.in_(line_ids)))).scalars().all()
    held: dict[int, dict[int, int]] = defaultdict(dict)
    rows = await db.execute(
        select(StockItemMovement.project_line_id, StockItemMovement.item_id, func.sum(StockItemMovement.delta_reserved))
        .where(StockItemMovement.project_line_id.in_(line_ids))
        .group_by(StockItemMovement.project_line_id, StockItemMovement.item_id)
    )
    for line_id, item_id, units in rows.all():
        if units:
            held[line_id][item_id] = int(units)
    parts_by_product = {pid: list(product.parts) for pid, product in products.items()}
    kits_of_line = {
        line_id: counted(comp) for line_id, comp in (await compositions_for_lines(db, lines, parts_by_product)).items()
    }
    kits = await part_stock.reserved_units_by_line(
        db, [line.id for line in lines], {lid: {part.id: per for part, per in kit} for lid, kit in kits_of_line.items()}
    )
    return {
        line.id: _LineBack(
            product_id=line.product_id,
            held_by_item=held.get(line.id, {}),
            issued=max(0, (line.from_finished or 0) - sum(held.get(line.id, {}).values())),
            kits=kits.get(line.id, 0),
            kit=kits_of_line.get(line.id, []),
        )
        for line in lines
    }


async def suggest(db: AsyncSession, requests: Sequence[PickRequest]) -> list[Suggestion]:
    """The proposal for each request, in the order asked."""
    products = await line_config.load_products(db, {r.product_id for r in requests})
    resolved = []
    for request in requests:
        product = products.get(request.product_id)
        if product is None:
            raise StockPickError("Product not found", 404)
        if request.quantity < 1:
            raise StockPickError("Quantity must be at least 1", 422)
        try:
            key, choices, counts = line_config.resolve_on(product, request.choices, request.counts)
        except line_config.LineConfigError as e:
            raise StockPickError(str(e), e.status) from e
        resolved.append((request, product, key, choices, counts))
    catalogue = [
        (product.id, key) for _r, product, key, _c, _n in resolved if product.origin == ProductOrigin.CATALOG.value
    ]
    positions = await finished_stock.positions_for_variants(db, catalogue)
    balances = await part_stock.balances_for_products(db, sorted(products))
    backs = await _lines_back(db, sorted({r.line_id for r in requests if r.line_id is not None}), products)
    out: list[Suggestion] = []
    for request, product, key, choices, counts in resolved:
        kit = (
            request.saved_kit
            if request.saved_kit is not None
            else composition(list(product.parts), "product", set(choices.values()), counts)
        )
        item = finished_stock.matching_item(positions.get(product.id, ()), key, snapshot_parts(kit))
        finished_free = item.on_hand - item.reserved if item is not None else 0
        shelf = balances.get(product.id, {})
        back = backs.get(request.line_id) if request.line_id is not None else None
        if back is not None and back.product_id == product.id:
            # What the line holds in THIS position, plus what it already issued, is its own.
            finished_free += (back.held_by_item.get(item.id, 0) if item is not None else 0) + back.issued
            if back.kits:
                shelf = dict(shelf)
                for part, per in back.kit:
                    shelf[part.id] = shelf.get(part.id, 0) + back.kits * per
        kits_free = part_stock.kits_of(shelf, kit)
        from_finished = min(request.quantity, finished_free)
        from_kits = min(request.quantity - from_finished, kits_free)
        out.append(
            Suggestion(
                product_id=product.id,
                finished_free=finished_free,
                kits_free=kits_free,
                from_finished=from_finished,
                from_kits=from_kits,
                to_print=request.quantity - from_finished - from_kits,
                position_id=item.id if item is not None else None,
                position_code=code_for("stock_item", item.id) if item is not None else None,
            )
        )
    return out
