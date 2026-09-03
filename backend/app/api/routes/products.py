"""Products (вироби) — catalog entities: composition, plate recipes, links.

Card fields, typed attachments, cover, export/import arrive in pass 4; the
columns already exist. Permissions: the projects family (spec §API).

⚠️ **No route here writes ``product_files`` or ``product_folders``.** The link
tables are owned by ``services/product_sync.py``, which keeps the pivot, the
``product_plates`` rows and the seeded parts in step with each other — three
things a route rewriting one table by hand would silently let drift apart. A
route's whole job is to work out what the file's (or folder's) FULL product set
should now be and hand that to the sync; the delta is the service's business.
The one exception is :func:`delete_product`, which drops its own pivot rows —
SQLite honours no ``ON DELETE CASCADE``, and there is no desired set left to
reconcile once the product itself is going away.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.product import Product, ProductPart, ProductPlate, product_files, product_folders
from backend.app.models.project_line import ProjectLine, ProjectProcurement
from backend.app.models.user import User
from backend.app.schemas.product import (
    FileLinkRequest,
    FolderLinkRequest,
    PlateRecipeResponse,
    PlateUnassignedEntry,
    PlateYieldEntry,
    ProductCreate,
    ProductDuplicate,
    ProductListItem,
    ProductPartAlias,
    ProductPartCreate,
    ProductPartMerge,
    ProductPartResponse,
    ProductPartUpdate,
    ProductResponse,
    ProductUpdate,
)
from backend.app.services.part_names import canonicalize, name_key
from backend.app.services.product_composition import (
    add_alias,
    merge_parts,
    purchased_name_key,
    recipe_for,
    remove_alias,
)
from backend.app.services.product_sync import apply_folder_products, sync_product_for_file

router = APIRouter(prefix="/products", tags=["products"])

_LOAD = (
    selectinload(Product.parts),
    selectinload(Product.plates),
    selectinload(Product.library_files),
    selectinload(Product.library_folders),
)


async def _get(db: AsyncSession, product_id: int) -> Product:
    product = (await db.execute(select(Product).options(*_LOAD).where(Product.id == product_id))).scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


async def _lines_count(db: AsyncSession, product_id: int) -> int:
    return (
        await db.execute(select(func.count(ProjectLine.id)).where(ProjectLine.product_id == product_id))
    ).scalar() or 0


async def _response(db: AsyncSession, product: Product) -> ProductResponse:
    # Two refreshes on purpose. The first lands the server-side ``created_at`` /
    # ``updated_at``: a row that was just INSERTed or UPDATEd leaves them
    # expired, and reading an expired attribute inside an async session is a
    # ``MissingGreenlet``, not a lazy SELECT. The second reloads the collections
    # the first expired — and picks up the pivot rows the sync wrote underneath
    # the ORM.
    await db.refresh(product)
    await db.refresh(product, ["parts", "plates", "library_files", "library_folders"])
    return ProductResponse(
        id=product.id,
        name=product.name,
        is_active=product.is_active,
        cover_image_filename=product.cover_image_filename,
        parts_count=len(product.parts),
        plates_count=len(product.plates),
        lines_count=await _lines_count(db, product.id),
        description=product.description,
        notes=product.notes,
        designer=product.designer,
        license=product.license,
        source_url=product.source_url,
        design_id=product.design_id,
        attachments=product.attachments,
        parts=[
            ProductPartResponse.model_validate(p) for p in sorted(product.parts, key=lambda p: (p.sort_order, p.id))
        ],
        library_file_ids=sorted(f.id for f in product.library_files),
        library_folder_ids=sorted(f.id for f in product.library_folders),
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


async def _file_product_ids(db: AsyncSession, file_id: int) -> set[int]:
    """Every product this file currently belongs to, read off the pivot itself.

    The sync is handed a FULL desired set, never a delta, so a route that adds
    or drops one product must first ask who else is on the file — otherwise the
    call evicts every other product from the pivot and takes their plates with it.
    """
    return set(
        (await db.execute(select(product_files.c.product_id).where(product_files.c.library_file_id == file_id)))
        .scalars()
        .all()
    )


async def _folder_product_ids(db: AsyncSession, folder_id: int) -> set[int]:
    """The folder twin of :func:`_file_product_ids`, same full-set reason."""
    return set(
        (await db.execute(select(product_folders.c.product_id).where(product_folders.c.library_folder_id == folder_id)))
        .scalars()
        .all()
    )


async def _apply_folder(db: AsyncSession, folder_id: int, product_ids: set[int]) -> None:
    """The folder door: it owns the folder's own ``product_folders`` row AND the
    ``product_files`` link of every child, then reconciles their plates."""
    try:
        await apply_folder_products(db, folder_id=folder_id, product_ids=sorted(product_ids))
    except ValueError as e:  # names product ids that do not exist; nothing was mutated
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("", response_model=list[ProductListItem])
@router.get("/", response_model=list[ProductListItem])
async def list_products(
    active: bool | None = None,
    q: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    query = select(Product).options(selectinload(Product.parts), selectinload(Product.plates)).order_by(Product.name)
    if active is not None:
        query = query.where(Product.is_active.is_(active))
    if q:
        query = query.where(Product.name.ilike(f"%{q.strip()}%"))
    products = (await db.execute(query)).scalars().all()
    counts = dict(
        (
            await db.execute(
                select(ProjectLine.product_id, func.count(ProjectLine.id)).group_by(ProjectLine.product_id)
            )
        ).all()
    )
    return [
        ProductListItem(
            id=p.id,
            name=p.name,
            is_active=p.is_active,
            cover_image_filename=p.cover_image_filename,
            parts_count=len(p.parts),
            plates_count=len(p.plates),
            lines_count=counts.get(p.id, 0),
        )
        for p in products
    ]


@router.post("", response_model=ProductResponse)
@router.post("/", response_model=ProductResponse)
async def create_product(
    data: ProductCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_CREATE),
):
    product = Product(**data.model_dump())
    db.add(product)
    await db.flush()
    return await _response(db, product)


@router.post("/from-file/{library_file_id}", response_model=ProductResponse)
async def create_product_from_file(
    library_file_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_CREATE),
):
    """'Print this file five times' must not require authoring a product."""
    file = (await db.execute(LibraryFile.active().where(LibraryFile.id == library_file_id))).scalar_one_or_none()
    if file is None:
        raise HTTPException(status_code=404, detail="Library file not found")
    stem = Path(file.filename).name
    for suffix in (".gcode.3mf", ".3mf", ".gcode"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    product = Product(name=stem or file.filename)
    db.add(product)
    await db.flush()
    desired = await _file_product_ids(db, file.id) | {product.id}
    await sync_product_for_file(db, library_file_id=file.id, product_ids=sorted(desired))
    return await _response(db, product)


@router.get("/{product_id}", response_model=ProductResponse)
async def get_product(
    product_id: int, db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.PROJECTS_READ)
):
    return await _response(db, await _get(db, product_id))


@router.patch("/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: int,
    data: ProductUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    product = await _get(db, product_id)
    for field_name in data.model_fields_set:  # explicit null clears; absent leaves alone
        setattr(product, field_name, getattr(data, field_name))
    await db.flush()
    return await _response(db, product)


@router.delete("/{product_id}")
async def delete_product(
    product_id: int, db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.PROJECTS_DELETE)
):
    product = await _get(db, product_id)
    if await _lines_count(db, product_id):
        raise HTTPException(status_code=409, detail="Product is used by an order line; remove the lines first")
    # SQLite honours no FK cascade, so nothing here leans on one: the pivot rows
    # and the procurement rows hanging off this product's parts are dropped by
    # hand, and the ORM cascades parts and plates.
    #
    # ⚠️ The pivots go through the COLLECTIONS, not a core DELETE. ``_get`` loads
    # both eagerly, so SQLAlchemy emits its own secondary DELETEs at flush; a
    # core DELETE racing them finds the row already gone and raises
    # ``StaleDataError`` — from the flush, far from the line that caused it.
    product.library_files = []
    product.library_folders = []
    await db.execute(
        delete(ProjectProcurement).where(
            ProjectProcurement.product_part_id.in_(select(ProductPart.id).where(ProductPart.product_id == product_id))
        )
    )
    await db.flush()
    await db.delete(product)
    return {"message": "Product deleted"}


@router.post("/{product_id}/duplicate", response_model=ProductResponse)
async def duplicate_product(
    product_id: int,
    data: ProductDuplicate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_CREATE),
):
    """Composition, aliases, links: a copy, never a move. Attachments follow in pass 4."""
    source = await _get(db, product_id)
    copy = Product(
        name=data.name or f"{source.name} (Copy)",
        description=source.description,
        notes=source.notes,
        designer=source.designer,
        license=source.license,
        source_url=source.source_url,
        design_id=source.design_id,
        is_active=True,
    )
    db.add(copy)
    await db.flush()
    for part in source.parts:
        db.add(
            ProductPart(
                product_id=copy.id,
                kind=part.kind,
                name=part.name,
                name_key=part.name_key,
                qty_per_unit=part.qty_per_unit,
                # NULL for a purchased part, and it stays NULL on the copy: the
                # column is printed-only, and [] would read as "no aliases yet".
                aliases=list(part.aliases) if part.aliases is not None else None,
                auto=part.auto,
                unit_price=part.unit_price,
                sourcing_url=part.sourcing_url,
                remarks=part.remarks,
                sort_order=part.sort_order,
            )
        )
    for plate in source.plates:
        db.add(ProductPlate(product_id=copy.id, library_file_id=plate.library_file_id, plate_index=plate.plate_index))
    for f in list(source.library_files):
        await sync_product_for_file(
            db, library_file_id=f.id, product_ids=sorted(await _file_product_ids(db, f.id) | {copy.id})
        )
    for folder in list(source.library_folders):
        await _apply_folder(db, folder.id, await _folder_product_ids(db, folder.id) | {copy.id})
    await db.flush()
    return await _response(db, copy)


# ---------- parts ----------


async def _part(db: AsyncSession, product: Product, part_id: int) -> ProductPart:
    part = next((p for p in product.parts if p.id == part_id), None)
    if part is None:
        raise HTTPException(status_code=404, detail="Part not found")
    return part


@router.post("/{product_id}/parts", response_model=ProductPartResponse)
async def create_part(
    product_id: int,
    data: ProductPartCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    product = await _get(db, product_id)
    if data.kind == "purchased":
        key = purchased_name_key(data.name)
    else:
        key = name_key(canonicalize(data.name))
    if any(key == p.name_key or key in (p.aliases or []) for p in product.parts):
        raise HTTPException(status_code=409, detail="A part with this name already exists")
    part = ProductPart(
        product_id=product.id,
        kind=data.kind,
        name=data.name.strip(),
        name_key=key,
        qty_per_unit=data.qty_per_unit,
        aliases=[key] if data.kind == "printed" else None,
        auto=False,
        unit_price=data.unit_price,
        sourcing_url=data.sourcing_url,
        remarks=data.remarks,
        sort_order=max((p.sort_order for p in product.parts), default=-1) + 1,
    )
    db.add(part)
    await db.flush()
    await db.refresh(part)
    return ProductPartResponse.model_validate(part)


@router.patch("/{product_id}/parts/{part_id}", response_model=ProductPartResponse)
async def update_part(
    product_id: int,
    part_id: int,
    data: ProductPartUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    part = await _part(db, await _get(db, product_id), part_id)
    for field_name in data.model_fields_set:
        setattr(part, field_name, getattr(data, field_name))
    # An operator edit is what ``auto`` exists to record: the seeded default is
    # no longer the answer, so the next sync must not touch this row's figures.
    part.auto = False
    await db.flush()
    await db.refresh(part)
    return ProductPartResponse.model_validate(part)


@router.delete("/{product_id}/parts/{part_id}")
async def delete_part(
    product_id: int,
    part_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    part = await _part(db, await _get(db, product_id), part_id)
    # ``project_procurement.product_part_id`` is ON DELETE CASCADE, which
    # PostgreSQL honours and SQLite does not — this codebase never sets
    # ``PRAGMA foreign_keys = ON``. Left behind, the row counts acquisitions
    # towards a part nothing can name any more.
    await db.execute(delete(ProjectProcurement).where(ProjectProcurement.product_part_id == part_id))
    await db.delete(part)
    return {"message": "Part deleted"}


@router.post("/{product_id}/parts/{part_id}/merge", response_model=ProductPartResponse)
async def merge_part(
    product_id: int,
    part_id: int,
    data: ProductPartMerge,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    product = await _get(db, product_id)
    target, source = await _part(db, product, part_id), await _part(db, product, data.source_part_id)
    if target is source:
        raise HTTPException(status_code=400, detail="A part cannot be merged into itself")
    merge_parts(target, source)
    # The source row goes away, so its procurement rows go with it — the same
    # FK-cascade reason as ``delete_part``, and deliberately NOT a transfer of
    # the acquired counts onto the target: PostgreSQL's cascade would drop them
    # anyway, and a backend-dependent answer here is worse than a plain one.
    await db.execute(delete(ProjectProcurement).where(ProjectProcurement.product_part_id == source.id))
    await db.delete(source)
    await db.flush()
    await db.refresh(target)
    return ProductPartResponse.model_validate(target)


@router.post("/{product_id}/parts/{part_id}/aliases", response_model=ProductPartResponse)
async def add_part_alias(
    product_id: int,
    part_id: int,
    data: ProductPartAlias,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    product = await _get(db, product_id)
    part = await _part(db, product, part_id)
    try:
        add_alias(product.parts, part, data.name_key.strip().lower())
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await db.flush()
    await db.refresh(part)
    return ProductPartResponse.model_validate(part)


@router.delete("/{product_id}/parts/{part_id}/aliases", response_model=ProductPartResponse)
async def remove_part_alias(
    product_id: int,
    part_id: int,
    name_key: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Query param dodges URL-encoding traps in part keys (same trick the old parts ledger used)."""
    part = await _part(db, await _get(db, product_id), part_id)
    try:
        remove_alias(part, name_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await db.flush()
    await db.refresh(part)
    return ProductPartResponse.model_validate(part)


# ---------- plates ----------


@router.get("/{product_id}/plates", response_model=list[PlateRecipeResponse])
async def list_plates(
    product_id: int, db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.PROJECTS_READ)
):
    product = await _get(db, product_id)
    files = (
        {
            f.id: f
            for f in (
                await db.execute(
                    select(LibraryFile).where(LibraryFile.id.in_({p.library_file_id for p in product.plates}))
                )
            ).scalars()
        }
        if product.plates
        else {}
    )
    names = {p.id: p.name for p in product.parts}
    out: list[PlateRecipeResponse] = []
    for plate in sorted(product.plates, key=lambda p: (p.library_file_id, p.plate_index)):
        file = files.get(plate.library_file_id)
        if file is None:
            continue
        r = recipe_for(plate, file.file_metadata, file.file_type, product.parts)
        out.append(
            PlateRecipeResponse(
                id=plate.id,
                library_file_id=plate.library_file_id,
                filename=file.filename,
                plate_index=plate.plate_index,
                sliced=r.sliced,
                **{
                    "yield": [
                        PlateYieldEntry(part_id=pid, name=names.get(pid, "?"), count=n)
                        for pid, n in sorted(r.yield_by_part.items())
                    ]
                },
                unassigned=[PlateUnassignedEntry(name_key=k, count=n) for k, n in sorted(r.unassigned.items())],
                materials=sorted(r.materials),
                colors=sorted(r.colors),
                print_time_seconds=r.print_time_seconds,
                filament_used_grams=r.filament_used_grams,
            )
        )
    return out


# ---------- links ----------


@router.put("/{product_id}/files", response_model=ProductResponse)
async def set_files(
    product_id: int,
    data: FileLinkRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    product = await _get(db, product_id)
    wanted = set(data.library_file_ids)
    found = (
        set((await db.execute(select(LibraryFile.id).where(LibraryFile.id.in_(wanted)))).scalars().all())
        if wanted
        else set()
    )
    if wanted - found:
        raise HTTPException(status_code=404, detail=f"Library files not found: {sorted(wanted - found)}")
    current = {f.id for f in product.library_files}
    # Only the files whose membership actually changes are touched, and each is
    # re-synced against its OWN full product set — never against this product
    # alone, which would evict every co-owner from the pivot.
    for fid in sorted(current ^ wanted):
        desired = await _file_product_ids(db, fid)
        desired = desired | {product_id} if fid in wanted else desired - {product_id}
        await sync_product_for_file(db, library_file_id=fid, product_ids=sorted(desired))
    return await _response(db, product)


@router.delete("/{product_id}/files/{file_id}", response_model=ProductResponse)
async def unlink_file(
    product_id: int,
    file_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    product = await _get(db, product_id)
    desired = await _file_product_ids(db, file_id) - {product_id}
    await sync_product_for_file(db, library_file_id=file_id, product_ids=sorted(desired))
    return await _response(db, product)


@router.put("/{product_id}/folders", response_model=ProductResponse)
async def set_folders(
    product_id: int,
    data: FolderLinkRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    product = await _get(db, product_id)
    wanted = set(data.library_folder_ids)
    found = (
        set((await db.execute(select(LibraryFolder.id).where(LibraryFolder.id.in_(wanted)))).scalars().all())
        if wanted
        else set()
    )
    if wanted - found:
        raise HTTPException(status_code=404, detail=f"Library folders not found: {sorted(wanted - found)}")
    current = {f.id for f in product.library_folders}
    for folder_id in sorted(current ^ wanted):
        desired = await _folder_product_ids(db, folder_id)
        desired = desired | {product_id} if folder_id in wanted else desired - {product_id}
        await _apply_folder(db, folder_id, desired)
    return await _response(db, product)


@router.delete("/{product_id}/folders/{folder_id}", response_model=ProductResponse)
async def unlink_folder(
    product_id: int,
    folder_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    product = await _get(db, product_id)
    await _apply_folder(db, folder_id, await _folder_product_ids(db, folder_id) - {product_id})
    return await _response(db, product)
