"""A product's stored plate facets — material, colour, printer model — ONE writer
(spec workshop-product-catalog, rules 3–7).

The catalog filters and searches by them in SQL, so they are stored rather than
parsed out of every linked file's JSON on each request (owner, 2026-09-26). The
plates they come from have one writer too (``product_sync``), and every rewrite
of a file's metadata (re-scan, a MakerWorld re-download, the objects backfill)
ends in that sync, so refreshing here whenever ``product_sync`` reconciles a
file keeps them true.

A row names the FILE it came from. The catalog reads only the rows of files
outside the trash (``visible_facet``; owner, 2026-09-27) — what the product's
plate list shows — so trashing and restoring, which do not pass through the
sync, need nothing from here.

Rows are replaced with Core statements, never ORM objects: a reader in the same
session may hold the old rows in its identity map, and a new object with the
same primary key would collide with them at flush. Id lists go to the database
in chunks of ``SQL_CHUNK`` — a whole catalog in one IN would pass asyncpg's
bind-parameter limit.
"""

from collections.abc import Iterable

from sqlalchemy import delete, exists, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductFacet, ProductPlate
from backend.app.services.product_composition import plate_colors, plate_materials
from backend.app.utils.printer_models import normalize_model_name

# ``product_facets.value`` is VARCHAR(64).
_VALUE_LENGTH = 64
SQL_CHUNK = 500


def id_chunks(ids: Iterable[int]):
    """``ids`` sorted and de-duplicated, ``SQL_CHUNK`` at a time."""
    ordered = sorted(set(ids))
    for start in range(0, len(ordered), SQL_CHUNK):
        yield ordered[start : start + SQL_CHUNK]


def visible_facet(*conditions):
    """EXISTS a facet row of the product in the query, from a file outside the trash."""
    return exists().where(
        ProductFacet.product_id == Product.id,
        LibraryFile.id == ProductFacet.library_file_id,
        LibraryFile.deleted_at.is_(None),
        *conditions,
    )


def facets_of(meta: dict | None, plate_index: int) -> set[tuple[str, str]]:
    """The facets one plate contributes, normalised as ``product_composition`` reads them."""
    out = {("material", m) for m in plate_materials(meta, plate_index)}
    out |= {("color", c) for c in plate_colors(meta, plate_index)}
    raw = (meta or {}).get("sliced_for_model")
    if isinstance(raw, str) and (model := normalize_model_name(raw)):
        out.add(("model", model))
    return {(kind, value[:_VALUE_LENGTH]) for kind, value in out if value}


async def refresh(db: AsyncSession, product_ids: Iterable[int]) -> None:
    """Recompute the facets of these products from their plates. Never commits."""
    for ids in id_chunks(product_ids):
        rows = (
            await db.execute(
                select(
                    ProductPlate.product_id,
                    ProductPlate.library_file_id,
                    ProductPlate.plate_index,
                    LibraryFile.file_metadata,
                )
                .join(LibraryFile, LibraryFile.id == ProductPlate.library_file_id)
                .where(ProductPlate.product_id.in_(ids))
            )
        ).all()
        wanted: set[tuple[int, int, str, str]] = set()
        for product_id, file_id, plate_index, meta in rows:
            wanted |= {(product_id, file_id, kind, value) for kind, value in facets_of(meta, plate_index)}
        await db.execute(delete(ProductFacet).where(ProductFacet.product_id.in_(ids)))
        if wanted:
            await db.execute(
                insert(ProductFacet),
                [
                    {"product_id": p, "library_file_id": f, "kind": kind, "value": value}
                    for p, f, kind, value in sorted(wanted)
                ],
            )


async def refresh_all(db: AsyncSession) -> None:
    await refresh(db, (await db.execute(select(Product.id))).scalars().all())


async def delete_for_product(db: AsyncSession, product_id: int) -> None:
    """SQLite runs no FK actions — the product's rows go in code."""
    await db.execute(delete(ProductFacet).where(ProductFacet.product_id == product_id))


async def delete_for_files(db: AsyncSession, file_ids: Iterable[int]) -> None:
    """Forget facets of purged files, including stale facet-only links. Never commits."""
    for ids in id_chunks(file_ids):
        await db.execute(delete(ProductFacet).where(ProductFacet.library_file_id.in_(ids)))
