"""Deleting a product — one implementation for the route and the order cascades.

The route (``DELETE /products/{id}``) refuses while a line references the
product. The cascades (deleting an order, deleting a line) run AFTER the lines
are gone and only for products the catalogue never saw (spec Decision 5): an
adhoc product lives exactly as long as a line references it.

SQLite honours no FK cascade, so nothing here leans on one: the pivot rows
and the procurement rows hanging off the product's parts are dropped by hand,
the stock ledger goes through its own writer, and the ORM cascades parts and
plates. Attachment files on disk are left as the route always left them —
``scripts/prune_orphan_archive_files.py`` reconciles disk.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.product import Product, ProductOrigin, ProductPart
from backend.app.models.project_line import ProjectLine, ProjectProcurement
from backend.app.services import part_stock


async def delete_product(db: AsyncSession, product: Product) -> None:
    """Delete ``product`` and everything that hangs off it.

    ``product.library_files`` / ``library_folders`` must be LOADED (the callers
    ``selectinload`` them): the pivots are cleared through the collections, so
    SQLAlchemy emits its own secondary DELETEs at flush — a core DELETE racing
    them raises ``StaleDataError`` from the flush.
    """
    product.library_files = []
    product.library_folders = []
    await db.execute(
        delete(ProjectProcurement).where(
            ProjectProcurement.product_part_id.in_(select(ProductPart.id).where(ProductPart.product_id == product.id))
        )
    )
    part_ids = (await db.execute(select(ProductPart.id).where(ProductPart.product_id == product.id))).scalars().all()
    await part_stock.delete_for_parts(db, list(part_ids))
    await db.flush()
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
