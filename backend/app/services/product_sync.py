"""The one door through which files join products (spec §Composition sync).

Successor of ``services/print_plan.py`` + ``services/project_parts.py``. Every
link path — direct link, folder link, folder inheritance on upload, folder
relink, product import — and every rewrite of ``file_metadata`` for a linked
file (external re-scan) ends up in :func:`sync_product_for_file`, which
guarantees three things for each product in ``product_ids``:

1. the ``product_files`` pivot holds exactly the links ``product_ids`` names —
   the file joins the products listed and leaves the ones it is not;
2. its ``product_plates`` rows for this file equal the file's wanted plate set
   (``{0}`` for ≤ 1 plate or no plate metadata, else the positive indices);
3. every object key on those plates resolves to exactly one printed part —
   an unknown key creates one (``aliases=[key]``, ``auto=True``,
   ``qty_per_unit`` = its count on the plate where it was first seen).

Products NOT in ``product_ids`` lose their plates for this file. Parts are
never deleted here: targets belong to the product, not the file.

⚠️ The pivot is reconciled as a DELTA against what is stored, never rewritten
blind. It is what lets :func:`resync_file_products` ask the pivot who is
linked: a file synced through this module is IN the pivot, not merely on some
plates.

⚠️ **The delta is order-dependent, and there are exactly two legal orders.**
Callers either

1. let the sync write the pivot — pass ``product_ids`` and, if the ORM
   collection is needed afterwards, ``await db.refresh(file, ["products"])``
   (this is the form the routes use); or
2. assign the collection FIRST and then sync — the read at the top autoflushes
   the pending INSERTs, so the delta comes out empty and nothing is written
   twice.

**Never sync and then assign.** The ORM would compute its delta against a
collection loaded before the sync ran and re-INSERT the pivot row the sync has
already written — ``product_files`` is a composite primary key, so that is an
``IntegrityError``, raised at flush time far from the line that caused it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import delete, insert, inspect as sqla_inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.product import Product, ProductOrigin, ProductPart, ProductPlate, product_files, product_folders
from backend.app.services.product_composition import part_index, plate_key_counts


def is_plan_eligible(file_type: str | None) -> bool:
    """Printable containers only: sliced ``.gcode.3mf`` (``file_type='gcode'``),
    raw gcode, and unsliced ``.3mf`` packages. STL/STEP/OBJ must be sliced first."""
    return bool(file_type) and file_type.lower() in ("3mf", "gcode")


def wanted_plate_indices(file_metadata: dict | None) -> set[int]:
    plates = (file_metadata or {}).get("plates") or []
    indices = {
        int(p["index"]) for p in plates if isinstance(p, dict) and isinstance(p.get("index"), int) and p["index"] > 0
    }
    return set(indices) if len(indices) > 1 else {0}


async def seed_parts_for_product(
    db: AsyncSession,
    *,
    product_id: int,
    meta: dict | None,
    plate_indices: Iterable[int],
    origin_plate_index: int | None = None,
) -> int:
    """Create a printed part for every object key on the given plates that no
    existing part (own key or alias) covers. Returns the number created.

    ``origin_plate_index`` is set for an ``adhoc_plate`` product (spec Decision
    4): a new part is seeded with its count on THAT plate, and 0 when it is not
    on it — the product measures one print of its plate and nothing else. An
    existing part is never re-counted here, whatever plate it came from.
    """
    existing = (await db.execute(select(ProductPart).where(ProductPart.product_id == product_id))).scalars().all()
    idx = part_index(existing)
    next_sort = max((p.sort_order for p in existing), default=-1) + 1
    origin_counts = plate_key_counts(meta, origin_plate_index)[0] if origin_plate_index is not None else None
    created = 0
    for plate_index in sorted(plate_indices):
        counts, display = plate_key_counts(meta, plate_index)
        for key, n in counts.items():
            if key in idx:
                continue
            part = ProductPart(
                product_id=product_id,
                kind="printed",
                name=display[key],
                name_key=key,
                qty_per_unit=n if origin_counts is None else origin_counts.get(key, 0),
                aliases=[key],
                auto=True,
                sort_order=next_sort,
            )
            db.add(part)
            # ⚠️ Indexed before it is flushed. Within one product a key belongs
            # to exactly one part; two plates carrying the same object would
            # otherwise each create one and collide on ``uq_product_parts_key``.
            idx[key] = part
            next_sort += 1
            created += 1
    return created


async def _linked_product_ids(db: AsyncSession, library_file_id: int) -> set[int]:
    """Every product this file belongs to, read off the pivot itself."""
    return set(
        (await db.execute(select(product_files.c.product_id).where(product_files.c.library_file_id == library_file_id)))
        .scalars()
        .all()
    )


async def _reconcile_links(db: AsyncSession, library_file_id: int, desired: set[int]) -> None:
    """Make ``product_files`` for this file equal ``desired``, as a delta."""
    linked = await _linked_product_ids(db, library_file_id)
    for product_id in sorted(desired - linked):
        await db.execute(insert(product_files).values(product_id=product_id, library_file_id=library_file_id))
    gone = linked - desired
    if gone:
        await db.execute(
            delete(product_files).where(
                product_files.c.library_file_id == library_file_id, product_files.c.product_id.in_(gone)
            )
        )


async def sync_product_for_file(db: AsyncSession, *, library_file_id: int, product_ids: list[int]) -> None:
    row = (
        await db.execute(
            select(LibraryFile.file_type, LibraryFile.file_metadata).where(LibraryFile.id == library_file_id)
        )
    ).first()
    if row is None:
        return
    file_type, meta = row
    desired = set(product_ids)
    await _reconcile_links(db, library_file_id, desired)

    existing = (
        (await db.execute(select(ProductPlate).where(ProductPlate.library_file_id == library_file_id))).scalars().all()
    )

    stale_products = {p.product_id for p in existing} - desired
    if stale_products:
        await db.execute(
            delete(ProductPlate).where(
                ProductPlate.library_file_id == library_file_id, ProductPlate.product_id.in_(stale_products)
            )
        )
    if not is_plan_eligible(file_type):
        # A geometry file still BELONGS to the product — it just has nothing to
        # print. Any plates it owns are left over from a slice it no longer is.
        if desired:
            await db.execute(
                delete(ProductPlate).where(
                    ProductPlate.library_file_id == library_file_id, ProductPlate.product_id.in_(desired)
                )
            )
        return

    wanted = wanted_plate_indices(meta)
    by_product: dict[int, dict[int, ProductPlate]] = {}
    for plate in existing:
        by_product.setdefault(plate.product_id, {})[plate.plate_index] = plate

    origins = {
        product.id: product.origin_plate_index if product.origin == ProductOrigin.ADHOC_PLATE.value else None
        for product in (await db.execute(select(Product).where(Product.id.in_(desired)))).scalars()
    }
    for product_id in desired:
        have = by_product.get(product_id, {})
        for plate_index, plate in have.items():
            if plate_index not in wanted:
                await db.delete(plate)
        for plate_index in sorted(wanted - have.keys()):
            db.add(ProductPlate(product_id=product_id, library_file_id=library_file_id, plate_index=plate_index))
        await seed_parts_for_product(
            db, product_id=product_id, meta=meta, plate_indices=wanted, origin_plate_index=origins.get(product_id)
        )
    await db.flush()


async def apply_folder_products(db: AsyncSession, *, folder_id: int, product_ids: list[int]) -> None:
    """Set a folder's product links and MERGE them into every child file.

    Owns BOTH sides: the folder's own ``product_folders`` link and the
    ``product_files`` link of every child. The folder's own row is not
    decoration — :func:`inherit_folder_products` reads ``folder.products`` to
    decide what the NEXT file dropped in here joins.

    ⚠️ **Merge on the children, never replace.** A child can belong to a product
    nobody linked through this folder — somebody linked the file itself. Each
    child therefore gets ``(its own products − the folder's PREVIOUS set) ∪ the
    folder's NEW set``: what the folder used to impose is withdrawn, what it now
    imposes is added, and a direct link to an unrelated product survives both.
    Assigning ``child.products = <the new set>`` instead evicted that product
    from the pivot, and :func:`sync_product_for_file` then deleted its plates
    for the file — a co-owner eviction, on the folder axis.

    ⚠️ Children go through :func:`sync_product_for_file` and nothing here else
    touches ``product_files``. The sync owns the pivot, the plates and the
    seeded parts together, and only it keeps the three in step.

    Raises ``ValueError`` naming the ids that do not exist — the route turns
    that into a 404, and the raise happens before anything is mutated.
    """
    product_rows: list[Product] = []
    if product_ids:
        product_rows = list((await db.execute(select(Product).where(Product.id.in_(product_ids)))).scalars().all())
        found = {p.id for p in product_rows}
        missing = [pid for pid in product_ids if pid not in found]
        if missing:
            raise ValueError(f"Product(s) not found: {missing}")

    new_ids = {p.id for p in product_rows}

    # ⚠️ ``selectinload``: assigning a collection that is not loaded lazy-loads
    # the old one first, and a lazy load inside an async session is a
    # ``MissingGreenlet``. It is also where ``previous`` comes from — read
    # before the assignment overwrites it.
    folder = (
        await db.execute(
            select(LibraryFolder).where(LibraryFolder.id == folder_id).options(selectinload(LibraryFolder.products))
        )
    ).scalar_one_or_none()
    previous: set[int] = set()
    if folder is not None:
        previous = {p.id for p in folder.products}
        folder.products = list(product_rows)

    child_ids = (await db.execute(select(LibraryFile.id).where(LibraryFile.folder_id == folder_id))).scalars().all()
    for child_id in child_ids:
        desired = (await _linked_product_ids(db, child_id) - previous) | new_ids
        await sync_product_for_file(db, library_file_id=child_id, product_ids=sorted(desired))


async def inherit_folder_products(db: AsyncSession, library_file: LibraryFile, folder: LibraryFolder | None) -> None:
    """A file created inside a product-linked folder joins those products.

    Defensive about unloaded relationships: touching a lazy collection inside
    an async session raises ``MissingGreenlet``, so both sides are refreshed
    when the caller did not eager-load them."""
    if folder is None:
        return
    if "products" in sqla_inspect(folder).unloaded:
        await db.refresh(folder, ["products"])
    folder_products = list(folder.products or [])
    if not folder_products:
        return
    if library_file.id is None:
        await db.flush()
    await db.refresh(library_file, ["products"])
    library_file.products = list(folder_products)
    await sync_product_for_file(db, library_file_id=library_file.id, product_ids=[p.id for p in folder_products])


async def purge_file_product_links(db: AsyncSession, library_file_ids: Sequence[int]) -> None:
    """Drop a file's product links and plates BEFORE its row is hard-deleted.

    ``product_files.library_file_id`` and ``product_plates.library_file_id``
    are both declared ``ON DELETE CASCADE``, which PostgreSQL honours and
    SQLite does not — this codebase never sets ``PRAGMA foreign_keys = ON``.
    Without this the pivot keeps a row pointing at a library file that is gone,
    and the product's plate list renders an entry nothing can open.

    Soft delete (the trash) must NOT call this: a trashed file is restorable,
    and its product links are part of what comes back.
    """
    ids = list(library_file_ids)
    if not ids:
        return
    await db.execute(
        update(Product).where(Product.origin_file_id.in_(list(library_file_ids))).values(origin_file_id=None)
    )
    await db.execute(delete(ProductPlate).where(ProductPlate.library_file_id.in_(ids)))
    await db.execute(delete(product_files).where(product_files.c.library_file_id.in_(ids)))


async def purge_folder_product_links(db: AsyncSession, folder_ids: Sequence[int]) -> None:
    """The folder twin of :func:`purge_file_product_links` — same FK reason."""
    ids = list(folder_ids)
    if not ids:
        return
    await db.execute(delete(product_folders).where(product_folders.c.library_folder_id.in_(ids)))


async def resync_file_products(db: AsyncSession, library_file_id: int) -> None:
    """Re-run the sync against the file's CURRENT links — for callers that
    rewrote ``file_metadata`` (re-scan) and changed nothing about the links."""
    ids = await _linked_product_ids(db, library_file_id)
    if ids:
        await sync_product_for_file(db, library_file_id=library_file_id, product_ids=sorted(ids))


__all__ = [
    "Product",
    "apply_folder_products",
    "inherit_folder_products",
    "is_plan_eligible",
    "purge_file_product_links",
    "purge_folder_product_links",
    "resync_file_products",
    "seed_parts_for_product",
    "sync_product_for_file",
    "wanted_plate_indices",
]
