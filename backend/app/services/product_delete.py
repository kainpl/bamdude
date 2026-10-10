"""Deleting a product — one implementation for the route and the order cascades.

The route (``DELETE /products/{id}``) refuses while a line references the
product. The cascades (deleting an order, deleting a line) run AFTER the lines
are gone and only for products the catalogue never saw (spec Decision 5): an
adhoc product lives exactly as long as a line references it.

SQLite honours no FK cascade, so nothing here leans on one: the pivot rows
and the procurement rows hanging off the product's parts are dropped by hand,
the stock ledger goes through its own writer, and the ORM cascades parts and
plates. Attachment files on disk are left as the route always left them —
``scripts/prune_orphan_archive_files.py`` reconciles disk. The parts' photos
(``part-images/``) go after the commit through part_images (part thumbnails, plan E4, D8).
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.finished_stock import StockItem
from backend.app.models.product import Product, ProductOrigin, ProductPart
from backend.app.models.project_line import ProjectLine, ProjectProcurement
from backend.app.services import finished_stock, part_images, part_stock, product_facets, stock_issues
from backend.app.services.product_gate import lock_product_delete, product_gate


async def delete_product(db: AsyncSession, product: Product) -> None:
    """Delete ``product`` and everything that hangs off it.

    ``product.library_files`` / ``library_folders`` must be LOADED (the callers
    ``selectinload`` them): the pivots are cleared through the collections, so
    SQLAlchemy emits its own secondary DELETEs at flush — a core DELETE racing
    them raises ``StaleDataError`` from the flush.
    """
    # The gate (re-entrant for the route and the order cascades, which take it at
    # their start) and everything that goes with the product, without waiting
    # (WS-13 E1 BL3 / BL5): a held row is ProductBusy, never a wait.
    await product_gate(db, [product.id])
    await lock_product_delete(db, product.id)
    # Finished goods first: a product still holding stock is refused before anything goes.
    await finished_stock.delete_for_product(db, product.id)
    # A note's lines keep their text; only the link goes (spec workshop-dispatch-notes, rule 8).
    await stock_issues.detach_product(db, product.id)
    product.library_files = []
    product.library_folders = []
    await db.execute(
        delete(ProjectProcurement).where(
            ProjectProcurement.product_part_id.in_(select(ProductPart.id).where(ProductPart.product_id == product.id))
        )
    )
    part_ids = (await db.execute(select(ProductPart.id).where(ProductPart.product_id == product.id))).scalars().all()
    await part_stock.delete_for_parts(db, list(part_ids))
    await product_facets.delete_for_product(db, product.id)
    await db.flush()
    part_images.forget_product(db, product.id)
    await db.delete(product)
    await db.flush()


async def delete_orphaned_adhoc_products(db: AsyncSession, product_ids: Iterable[int]) -> list[int]:
    """Delete every NON-catalogue product in ``product_ids`` that no order line
    references any more. Returns the ids that went. Call it after the lines
    that referenced them are flushed away."""
    ids = set(product_ids)
    if not ids:
        return []
    still_used = set(
        (await db.execute(select(ProjectLine.product_id).where(ProjectLine.product_id.in_(ids)))).scalars().all()
    )
    # A one-off product whose printed units went through the shelf keeps its position for
    # good (spec workshop-order-issue, rule 14): units given back are real goods, and the
    # movements of units issued ARE the issues' units and the stock journal — deleting
    # the product would take them along (``delete_for_product``).
    still_used |= set(
        (await db.execute(select(StockItem.product_id).where(StockItem.product_id.in_(ids)))).scalars().all()
    )
    orphans = (
        (
            await db.execute(
                select(Product)
                .options(
                    selectinload(Product.library_files),
                    selectinload(Product.library_folders),
                    selectinload(Product.parts),
                    selectinload(Product.plates),
                )
                .where(Product.id.in_(ids - still_used), Product.origin != ProductOrigin.CATALOG.value)
            )
        )
        .scalars()
        .all()
    )
    for product in orphans:
        await delete_product(db, product)
    return [product.id for product in orphans]
