"""Products (вироби) — catalog entities: composition, plate recipes, links.

Card fields, typed attachments, the cover and export/import all live here;
the columns behind them predate the routes. Permissions: the projects family
(spec §API).

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

import asyncio
import logging
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import delete, func, inspect as sqla_inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile as StarletteUploadFile

from backend.app.core.auth import RequireCameraStreamToken, RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.product import Product, ProductPart, ProductPlate, product_files, product_folders
from backend.app.models.project_line import ProjectLine, ProjectProcurement
from backend.app.models.user import User
from backend.app.schemas.product import (
    AttachmentOrderRequest,
    CoverPickRequest,
    FileLinkRequest,
    FolderLinkRequest,
    PlateRecipeResponse,
    PlateUnassignedEntry,
    PlateYieldEntry,
    ProductAttachmentOut,
    ProductCreate,
    ProductDuplicate,
    ProductImportResponse,
    ProductListItem,
    ProductPartAlias,
    ProductPartCreate,
    ProductPartMerge,
    ProductPartResponse,
    ProductPartUpdate,
    ProductResponse,
    ProductUpdate,
    RereadResponse,
)
from backend.app.services.part_names import canonicalize, name_key
from backend.app.services.product_card import (
    export_zip,
    fill_from_file,
    import_zip,
    read_card,
    units_printed_total,
    usable_title,
)
from backend.app.services.product_composition import (
    add_alias,
    merge_parts,
    purchased_name_key,
    recipes_for_product,
    remove_alias,
)
from backend.app.services.product_files import (
    ATTACHMENT_CATEGORIES,
    CATEGORY_EXTENSIONS,
    COVER_EXTENSIONS,
    SOURCE_MANUAL,
    attachment_entry,
    attachment_limit,
    category_entries,
    effective_cover,
    exceeds_attachment_limit,
    image_media_type,
    import_limit,
    next_sort_order,
    product_attachments_dir,
    safe_attachment_name,
    sorted_attachments,
)
from backend.app.services.product_sync import apply_folder_products, sync_product_for_file
from backend.app.utils.http import build_content_disposition

logger = logging.getLogger(__name__)

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


async def _plates_count(db: AsyncSession, product_ids: list[int]) -> dict[int, int]:
    """Plates per product, counting only those a printer could still be sent.

    ⚠️ NOT ``len(product.plates)``. A trashed file is restorable, so its
    ``product_plates`` rows stay — and ``recipes_for_product`` (which is what
    ``GET /plates`` lists and what the plan engine offers) drops them. Counting
    the links instead made the card promise plates the plate list did not show.
    One grouped query, so the list endpoint pays for it once.
    """
    if not product_ids:
        return {}
    rows = await db.execute(
        select(ProductPlate.product_id, func.count(ProductPlate.id))
        .join(LibraryFile, LibraryFile.id == ProductPlate.library_file_id)
        .where(ProductPlate.product_id.in_(product_ids), LibraryFile.deleted_at.is_(None))
        .group_by(ProductPlate.product_id)
    )
    return dict(rows.all())


async def _timestamps(db: AsyncSession, product: Product) -> tuple[datetime, datetime]:
    """``created_at`` / ``updated_at`` come from server-side defaults, so an
    INSERT or an UPDATE leaves them expired — and reading an expired attribute
    inside an async session is a ``MissingGreenlet``, not a lazy SELECT.

    ⚠️ A plain SELECT, never ``db.refresh(product, ["created_at", ...])``: a
    partial refresh ALSO unloads the collections ``_get`` eager-loaded (and the
    empty ones a freshly built row starts with), so the caller would have to
    reload all four just to count them. Nothing is read when nothing expired,
    which is the ordinary GET.
    """
    if not ({"created_at", "updated_at"} & sqla_inspect(product).unloaded):
        return product.created_at, product.updated_at
    row = (await db.execute(select(Product.created_at, Product.updated_at).where(Product.id == product.id))).one()
    return row[0], row[1]


async def _response(db: AsyncSession, product: Product, *, reload_links: bool = False) -> ProductResponse:
    links = ["parts", "plates", "library_files", "library_folders"]
    # ``reload_links`` — a sync ran, and it wrote ``product_files`` /
    # ``product_plates`` with core SQL underneath the ORM, so what is loaded is
    # stale. ``unloaded`` — a row built and flushed in this request never had
    # its collections initialised, and touching one now would be a lazy load,
    # i.e. a ``MissingGreenlet``. A plain GET or PATCH is neither, and pays for
    # neither: ``_get`` already eager-loaded all four.
    if reload_links or set(links) & sqla_inspect(product).unloaded:
        await db.refresh(product, links)
    created_at, updated_at = await _timestamps(db, product)
    return ProductResponse(
        id=product.id,
        name=product.name,
        is_active=product.is_active,
        cover_image_filename=product.cover_image_filename,
        has_cover=effective_cover(product) is not None,
        parts_count=len(product.parts),
        plates_count=(await _plates_count(db, [product.id])).get(product.id, 0),
        lines_count=await _lines_count(db, product.id),
        description=product.description,
        notes=product.notes,
        designer=product.designer,
        license=product.license,
        source_url=product.source_url,
        design_id=product.design_id,
        attachments=sorted_attachments(product),
        parts=[
            ProductPartResponse.model_validate(p) for p in sorted(product.parts, key=lambda p: (p.sort_order, p.id))
        ],
        library_file_ids=sorted(f.id for f in product.library_files),
        library_folder_ids=sorted(f.id for f in product.library_folders),
        units_printed_total=await units_printed_total(db, product.id),
        created_at=created_at,
        updated_at=updated_at,
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
    plates = await _plates_count(db, [p.id for p in products])
    return [
        ProductListItem(
            id=p.id,
            name=p.name,
            is_active=p.is_active,
            cover_image_filename=p.cover_image_filename,
            has_cover=effective_cover(p) is not None,
            parts_count=len(p.parts),
            plates_count=plates.get(p.id, 0),
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
    """'Print this file five times' must not require authoring a product.

    The card names it when it can: a 3MF's ``Title`` beats a filename, EXCEPT
    when it is one of BambuStudio's placeholders, in which case the stem stands
    (spec §Risks, "``Title`` ≠ name"). The parse is handed on to
    :func:`fill_from_file` so the ZIP is opened once, not twice.

    ⚠️ **The fill's notes go to the LOG, not the wire.** ``ProductResponse`` has
    no ``notes`` field and giving it one would change the shape every product
    endpoint answers with, for the benefit of this one route — the re-read
    endpoint has ``RereadResponse`` for exactly that. But the notes are the only
    record of what a designer's file did not give up (a picture over the size
    cap, an ``.exe`` in ``Others/``), so throwing them away left an operator
    with a half-filled product and nothing to read. Codes and params, never
    prose: nothing here knows the operator's language.
    """
    file = (await db.execute(LibraryFile.active().where(LibraryFile.id == library_file_id))).scalar_one_or_none()
    if file is None:
        raise HTTPException(status_code=404, detail="Library file not found")
    stem = Path(file.filename).name
    for suffix in (".gcode.3mf", ".3mf", ".gcode"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    card = await read_card(file)
    product = Product(name=usable_title(card) or stem or file.filename)
    db.add(product)
    await db.flush()
    desired = await _file_product_ids(db, file.id) | {product.id}
    await sync_product_for_file(db, library_file_id=file.id, product_ids=sorted(desired))
    for note in await fill_from_file(db, product, file, replace_3mf_attachments=False, card=card):
        logger.info("Product %s from library file %s: code=%s params=%s", product.id, file.id, note.code, note.params)
    return await _response(db, product, reload_links=True)


# ---------- export / import (spec §Decisions 6) ----------
#
# ⚠️ Declared BEFORE every ``/{product_id}``-shaped route below. Nothing in this
# router matches ``POST /products/import`` today — the id-shaped POSTs all carry
# a second segment — but the day somebody adds ``POST /products/{product_id}``
# the literal has to already be in front of it, or FastAPI hands "import" to an
# ``int`` path parameter and answers 422 to a working request.
#
# ⚠️ **Neither route ever holds an archive in memory.** A product's export
# carries every 3MF it prints from; on the farm this was built for that is
# already hundreds of megabytes, and the machine serving it is routinely a Pi.
# The export builds a temp file and streams it; the import reads members out of
# the file the multipart parser has already spooled.


def _measure_upload(file: UploadFile) -> int:
    """How big the part the multipart parser already wrote actually is.

    ⚠️ There is nothing to stream here, and an earlier version of this route
    pretended otherwise: by the time a handler runs, FastAPI has consumed the
    body and Starlette has put each file part in a ``SpooledTemporaryFile``
    (memory up to a megabyte, a real file above that). Reading it again in
    chunks into a temp file of our own copied a file that already existed and
    could not refuse anything "before receipt" — receipt had happened.

    So this measures what we hold, by seeking. It is a fact rather than the
    client's word, which is what makes it the gate that counts; the declared
    ``Content-Length`` is checked before it purely as a cheap refusal before OUR
    work starts.
    """
    handle = file.file
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(0)
    return size


@router.post("/import", response_model=ProductImportResponse)
async def import_product(
    request: Request,
    file: UploadFile = File(...),
    folder_id: int | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
    # Both permissions, and the library's own is not decoration: an import
    # INGESTS FILES INTO THE LIBRARY. A caller who may create products but may
    # not upload must not gain an upload route by wrapping the bytes in a
    # product archive.
    current_user: User | None = RequirePermission(Permission.PROJECTS_CREATE, Permission.LIBRARY_UPLOAD),
):
    declared = request.headers.get("content-length") or ""
    if declared.isdigit() and int(declared) > import_limit():
        raise HTTPException(status_code=413, detail=f"An import may be at most {import_limit()} bytes")
    if _measure_upload(file) > import_limit():
        raise HTTPException(status_code=413, detail=f"An import may be at most {import_limit()} bytes")
    # ``file.file`` is seekable, which is all ``zipfile`` asks for — so the
    # archive is read where it already is, member by member.
    product, warnings = await import_zip(db, file.file, folder_id=folder_id, user=current_user)
    return ProductImportResponse(product=await _response(db, product, reload_links=True), warnings=warnings)


@router.get("/{product_id}/export")
async def export_product(
    product_id: int, db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.PROJECTS_READ)
):
    """The product as a ZIP: ``product.json``, its files, its attachments.

    ``BackgroundTask``, not a ``finally``: the archive is a temp file and
    ``FileResponse`` has not read a byte of it when this handler returns, so
    deleting it here would serve an empty download. Starlette runs the task once
    the response has been sent.
    """
    archive = await export_zip(db, await _get(db, product_id))
    return FileResponse(
        archive.path,
        media_type="application/zip",
        headers={
            "Content-Disposition": build_content_disposition(archive.filename, ascii_fallback=archive.ascii_filename)
        },
        background=BackgroundTask(os.unlink, archive.path),
    )


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


def _copy_attachment_files(
    source_id: int, new_id: int, entries: list[dict], cover: str | None
) -> tuple[list[dict], str | None]:
    """Copy one product's attachment files to another and return the new rows.

    ⚠️ **Blocking, and it runs in ONE thread hop.** Copying is the only work
    here and it is all filesystem; splitting it per file would only multiply the
    hops. It touches no ORM object — the rows arrive as plain dicts and leave as
    plain dicts, because a lazy attribute read off the event loop is a
    ``MissingGreenlet``, not a SELECT.

    ⚠️ **Fresh stored names, not a directory copy.** ``projects`` duplicates by
    ``copytree`` and keeps the names; here every entry gets a new ``uuid4``, so
    the two products' galleries can never end up naming the same file — which is
    what would let deleting one take the other's pictures with it. The
    ``original_name`` the operator gave is kept exactly as it was.

    ⚠️ **The dedicated cover is not a gallery entry**, so it is copied
    separately or the copy would carry a column pointing at nothing. A cover
    that IS a gallery picture is remapped to that picture's new name instead.

    A source file whose bytes are gone is dropped rather than carried as a row
    that resolves to nothing: an entry the copy cannot show is worse than no
    entry, and the source still has whatever it still has.
    """
    src_dir = product_attachments_dir(source_id)
    dst_dir = product_attachments_dir(new_id)
    rows: list[dict] = []
    stamp = datetime.now(UTC).isoformat()
    renamed: dict[str, str] = {}

    def carry(stored: str, prefix: str = "") -> str | None:
        try:
            safe_attachment_name(stored)
        except HTTPException:  # a hand-edited row; its file is not ours to guess at
            return None
        source_path = src_dir / stored  # SEC-PATH-OK: guarded by safe_attachment_name just above
        if not source_path.is_file():
            return None
        fresh = f"{prefix}{uuid.uuid4().hex}{os.path.splitext(stored)[1].lower()}"
        try:
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(
                source_path, dst_dir / fresh
            )  # SEC-PATH-OK: 'cover_'? + uuid4().hex + the source's own extension
        except OSError as e:
            logger.warning("Product %s: attachment %s could not be copied from %s: %s", new_id, stored, source_id, e)
            return None
        renamed[stored] = fresh
        return fresh

    for entry in entries:
        stored = entry.get("filename")
        fresh = carry(stored) if isinstance(stored, str) else None
        if fresh is None:
            continue
        rows.append({**entry, "filename": fresh, "uploaded_at": stamp})

    if not cover:
        return rows, None
    if cover in renamed:
        return rows, renamed[cover]
    # Not in the gallery: a dedicated upload, carried under its own prefix so
    # `_drop_dedicated_cover` still recognises it on the copy.
    return rows, carry(cover, prefix="cover_")


@router.post("/{product_id}/duplicate", response_model=ProductResponse)
async def duplicate_product(
    product_id: int,
    data: ProductDuplicate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_CREATE),
):
    """Composition, aliases, links, pictures and documents: a copy, never a move.

    The attachment FILES are copied too, not just the column — see
    :func:`_copy_attachment_files`. A copy holding the source's filenames would
    look right until somebody deleted the source, and then show a gallery of
    broken tiles.
    """
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
    # ⚠️ No hand-copied ``ProductPlate`` rows. The syncs below plant the plates
    # for every file the copy ends up linked to, from the file's own metadata —
    # copying them here as well only avoided ``uq_product_plates_file_plate``
    # because autoflush happened to run before the sync read them.
    for f in list(source.library_files):
        await sync_product_for_file(
            db, library_file_id=f.id, product_ids=sorted(await _file_product_ids(db, f.id) | {copy.id})
        )
    for folder in list(source.library_folders):
        await _apply_folder(db, folder.id, await _folder_product_ids(db, folder.id) | {copy.id})

    # Read the JSON column into plain dicts HERE, on the loop, before the thread
    # touches anything: the worker gets data, never an ORM row.
    copy.attachments, copy.cover_image_filename = await asyncio.to_thread(
        _copy_attachment_files,
        source.id,
        copy.id,
        sorted_attachments(source),
        source.cover_image_filename,
    )
    await db.flush()
    return await _response(db, copy, reload_links=True)


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
    product = await _get(db, product_id)
    part = await _part(db, product, part_id)
    # A PURCHASED part IS its name — ``name_key`` is derived from it (there is no
    # 3MF object to key off), so a rename that left the key behind made the two
    # disagree for good and let a second "M4 screw" be created beside the first.
    # A PRINTED part's key is the object name inside the file and must survive
    # every rename: refreshing it would orphan the archive rows that match on it.
    new_key = None
    if "name" in data.model_fields_set and part.kind == "purchased":
        new_key = purchased_name_key(data.name)
        if new_key != part.name_key and any(
            p is not part and (new_key == p.name_key or new_key in (p.aliases or [])) for p in product.parts
        ):
            raise HTTPException(status_code=409, detail="A part with this name already exists")
    for field_name in data.model_fields_set:
        setattr(part, field_name, getattr(data, field_name))
    if new_key is not None:
        # The procurement rows reference the part ID, so nothing of the order
        # moves with the key — there is no migration here, only a stale value.
        part.name_key = new_key
    if data.model_fields_set:
        # An operator edit is what ``auto`` exists to record: the seeded default
        # is no longer the answer, so the next sync must not touch this row's
        # figures. An empty body edited nothing and must not clear the flag.
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
    if part.kind == "purchased":
        # ``ProductPart.aliases`` is printed-only by the model's contract: an
        # alias maps a 3MF object name onto a part, and nothing on a plate is
        # ever a purchased screw.
        raise HTTPException(status_code=400, detail="Purchased parts have no aliases")
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
    names = {p.id: p.name for p in product.parts}
    out: list[PlateRecipeResponse] = []
    # ``recipes_for_product`` is the shared loop (it drops a trashed file's
    # plates); it hands rows back in plate-id order, this list is by file then
    # plate for the operator.
    rows = await recipes_for_product(db, product)
    for plate, file, r in sorted(rows, key=lambda row: (row[0].library_file_id, row[0].plate_index)):
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
    return await _response(db, product, reload_links=True)


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
    return await _response(db, product, reload_links=True)


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
    return await _response(db, product, reload_links=True)


@router.delete("/{product_id}/folders/{folder_id}", response_model=ProductResponse)
async def unlink_folder(
    product_id: int,
    folder_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    product = await _get(db, product_id)
    await _apply_folder(db, folder_id, await _folder_product_ids(db, folder_id) - {product_id})
    return await _response(db, product, reload_links=True)


# ---------- the model card (spec §Decisions 2) ----------


async def _linked_file(db: AsyncSession, product: Product, file_id: int) -> LibraryFile:
    """The file, when this product really holds it — directly or through a folder.

    A folder link is mirrored onto every child's ``product_files`` row by
    ``apply_folder_products``, so the pivot alone answers both cases for every
    file the sync has seen. The folder check behind it is not redundant: a file
    that landed in a linked folder without a sync (a restored row, a scan that
    has not run) is still the operator's to re-read from.
    """
    file = (await db.execute(LibraryFile.active().where(LibraryFile.id == file_id))).scalar_one_or_none()
    if file is not None and product.id in await _file_product_ids(db, file.id):
        return file
    if file is not None and file.folder_id is not None:
        if product.id in await _folder_product_ids(db, file.folder_id):
            return file
    # 404, not 403: whether a stranger's file exists is not this route's to say.
    raise HTTPException(status_code=404, detail="That file is not linked to this product")


@router.post("/{product_id}/card/reread", response_model=RereadResponse)
async def reread_card(
    product_id: int,
    file_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Read the card out of a linked file again.

    Blank fields are filled and this file's previous ``source = "3mf"``
    attachments are replaced; everything the operator wrote or uploaded is left
    alone (spec §Decisions 2). Linking a file does NOT do this on its own —
    the page offers it, so re-reading is always something somebody asked for.
    """
    product = await _get(db, product_id)
    file = await _linked_file(db, product, file_id)
    notes = await fill_from_file(db, product, file, replace_3mf_attachments=True)
    await db.flush()
    return RereadResponse(product=await _response(db, product), notes=notes)


# ---------- typed attachments (spec §Decisions 3) ----------
#
# One JSON list on the row, the files under ``archive_dir/products/<id>/attachments``.
# ⚠️ ``Product.attachments`` is a plain JSON column, so every writer below
# ASSIGNS a new list — mutating the loaded one in place is invisible to the
# flush and the write is silently lost.


@router.get("/{product_id}/attachments", response_model=list[ProductAttachmentOut])
async def list_attachments(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    return sorted_attachments(await _get(db, product_id))


@router.post("/{product_id}/attachments", response_model=ProductAttachmentOut)
async def upload_attachment(
    product_id: int,
    file: UploadFile = File(...),
    category: str = Form(...),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """⚠️ ``CATEGORY_EXTENSIONS[category]`` is the only defence against an
    executable landing in the attachments directory (spec §Risks) — the category
    is checked first precisely so the lookup can never fall back to "anything"."""
    product = await _get(db, product_id)
    if category not in ATTACHMENT_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Category must be one of {list(ATTACHMENT_CATEGORIES)}")
    original_name = file.filename or "unknown"
    ext = os.path.splitext(original_name)[1].lower()
    allowed = CATEGORY_EXTENSIONS[category]
    if ext not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"'{ext or original_name}' is not allowed in {category}. Allowed: {sorted(allowed)}",
        )

    # The declared size first when the client sent one, so an oversized body is
    # refused before it is buffered; the read is checked again because that
    # header is the client's word, not a fact.
    if exceeds_attachment_limit(getattr(file, "size", None)):
        raise HTTPException(status_code=413, detail=f"An attachment may be at most {attachment_limit()} bytes")

    directory = product_attachments_dir(product_id)
    directory.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}{ext}"
    path = (
        directory / stored
    )  # SEC-PATH-OK: stored = uuid4().hex + an extension validated against this category's allowlist just above
    content = await file.read()
    if exceeds_attachment_limit(len(content)):
        raise HTTPException(status_code=413, detail=f"An attachment may be at most {attachment_limit()} bytes")
    try:
        # Off the loop: an attachment is up to 50 MB and a write to a NAS-backed
        # archive directory blocks for as long as that mount feels like.
        await asyncio.to_thread(path.write_bytes, content)
    except OSError as e:
        logger.error("Failed to save product attachment %s: %s", path, e)
        raise HTTPException(status_code=500, detail="Failed to save attachment") from e

    entry = {
        "category": category,
        "filename": stored,
        "original_name": original_name,
        "size": len(content),
        "sort_order": next_sort_order(product, category),
        "source": SOURCE_MANUAL,
        # UTC and aware — see the same stamp in ``product_card``.
        "uploaded_at": datetime.now(UTC).isoformat(),
    }
    product.attachments = [*(product.attachments or []), entry]
    await db.flush()
    return entry


@router.patch("/{product_id}/attachments/order", response_model=list[ProductAttachmentOut])
async def reorder_attachments(
    product_id: int,
    data: AttachmentOrderRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """The gallery order is data, not a render-time sort (parent spec).

    ``sort_order`` is per category, so this rewrites ONE category and leaves the
    others alone. A filename from another category is a 400 rather than a silent
    no-op: it means the caller and the server disagree about what is where.
    """
    product = await _get(db, product_id)
    if data.category not in ATTACHMENT_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Category must be one of {list(ATTACHMENT_CATEGORIES)}")
    mine = [a["filename"] for a in category_entries(product, data.category)]
    strangers = [f for f in data.filenames if f not in mine]
    if strangers:
        raise HTTPException(status_code=400, detail=f"Not attachments of '{data.category}': {sorted(strangers)}")
    if len(set(data.filenames)) != len(data.filenames):
        raise HTTPException(status_code=400, detail="The same filename appears twice in the order")

    ranked = {filename: i for i, filename in enumerate(data.filenames)}
    # A partial order is legal: whatever was not named keeps its relative order
    # behind what was, so a drag of one thumbnail need not resend the gallery.
    for i, filename in enumerate((f for f in mine if f not in ranked), start=len(ranked)):
        ranked[filename] = i
    product.attachments = [
        {**a, "sort_order": ranked[a["filename"]]}
        if isinstance(a, dict) and a.get("category") == data.category and a.get("filename") in ranked
        else a
        for a in (product.attachments or [])
    ]
    await db.flush()
    return sorted_attachments(product)


@router.get("/{product_id}/attachments/{filename}")
async def download_attachment(
    product_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """Bearer-authenticated, and it gives the operator's own name back."""
    safe_attachment_name(filename)
    product = await _get(db, product_id)
    entry = attachment_entry(product, filename)
    if entry is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    path = (
        product_attachments_dir(product_id) / filename
    )  # SEC-PATH-OK: filename is rejected for separators, .. and empty just above the join
    if not path.exists():
        raise HTTPException(status_code=404, detail="Attachment file not found")
    # ⚠️ The header is BUILT, never handed to Starlette as ``filename=``: that
    # parameter is encoded latin-1, and an attachment an operator named in
    # Ukrainian would raise ``UnicodeEncodeError`` from deep inside the response
    # instead of downloading. Same helper as ``card-download`` and the export.
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"Content-Disposition": build_content_disposition(entry.get("original_name") or filename)},
    )


@router.get("/{product_id}/attachment-image/{filename}")
async def get_attachment_image(
    product_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
    _=RequireCameraStreamToken,
):
    """Pictures for ``<img src>``, which cannot carry an Authorization header —
    so this takes the same ``?token=`` credential as the project cover route.

    ⚠️ ``/attachment-image/`` is a UNIQUE segment on purpose, and it is listed in
    ``main.py``'s ``PUBLIC_API_PATTERNS``. ``auth_middleware`` runs BEFORE any
    route dependency, so without that entry every request would be 401'd by the
    middleware and never reach this route's own stream-token gate — the route
    would be dead for the only client that needs it, a browser ``<img>``. The
    whitelist entry lets the request REACH the gate; it does not open the route.
    Nesting it under ``/attachments/`` would have needed a pattern that also
    matched the bearer-only download beside it.

    Pictures ONLY: a bom_docs PDF is not served through a token surface just
    because it happens to be attached. The stored name is a uuid and never
    changes, so an hour of PRIVATE browser cache is safe here — it would NOT be
    on ``/cover-image``, whose URL survives the cover being replaced.
    """
    safe_attachment_name(filename)
    product = await _get(db, product_id)
    entry = attachment_entry(product, filename)
    if entry is None or entry.get("category") != "pictures":
        raise HTTPException(status_code=404, detail="Picture not found")
    path = (
        product_attachments_dir(product_id) / filename
    )  # SEC-PATH-OK: filename is rejected for separators, .. and empty just above the join
    if not path.exists():
        raise HTTPException(status_code=404, detail="Picture file not found")
    # ``private``: this is one operator's data behind a token, never a shared
    # cache's to keep.
    return FileResponse(path, media_type=image_media_type(filename), headers={"Cache-Control": "private, max-age=3600"})


@router.delete("/{product_id}/attachments/{filename}", response_model=list[ProductAttachmentOut])
async def delete_attachment(
    product_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    safe_attachment_name(filename)
    product = await _get(db, product_id)
    if attachment_entry(product, filename) is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    product.attachments = [
        a for a in (product.attachments or []) if not (isinstance(a, dict) and a.get("filename") == filename)
    ]
    # The cover column may point at exactly this picture; leaving it would be a
    # dangling reference for someone else's request to heal.
    if product.cover_image_filename == filename:
        product.cover_image_filename = None
    path = (
        product_attachments_dir(product_id) / filename
    )  # SEC-PATH-OK: filename is rejected for separators, .. and empty just above the join
    if path.exists():
        try:
            path.unlink()
        except OSError as e:
            logger.warning("Failed to delete product attachment file %s: %s", path, e)
    await db.flush()
    return sorted_attachments(product)


# ---------- the cover (spec §Decisions 4) ----------


def _drop_dedicated_cover(product: Product, directory: Path) -> None:
    """Delete the current cover file when NOTHING but the column references it.

    A ``cover_<uuid>`` upload is not a gallery entry, so replacing or clearing
    the column strands its file. A picked gallery picture is not ours to delete —
    the gallery still shows it.
    """
    current = product.cover_image_filename
    if not current or attachment_entry(product, current) is not None:
        return
    try:
        safe_attachment_name(current)
    except HTTPException:  # a hand-edited row; leave the file alone
        return
    path = directory / current  # SEC-PATH-OK: guarded by safe_attachment_name just above
    if path.exists():
        try:
            path.unlink()
        except OSError as e:
            logger.warning("Failed to delete the previous product cover %s: %s", path, e)


@router.put("/{product_id}/cover-image")
async def set_product_cover_image(
    product_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Two bodies on one path (spec §Decisions 4).

    JSON ``{filename}`` PICKS a picture already in the gallery; a multipart
    ``file`` UPLOADS a dedicated cover stored beside the gallery and deliberately
    not listed in it. Both write the same column, so the page has one route to
    call whichever the operator chose.
    """
    product = await _get(db, product_id)
    directory = product_attachments_dir(product_id)

    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, StarletteUploadFile):
            raise HTTPException(status_code=400, detail="A multipart body must carry 'file'")
        original_name = upload.filename or "cover"
        ext = os.path.splitext(original_name)[1].lower()
        if ext not in COVER_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Cover image must be one of {sorted(COVER_EXTENSIONS)}")
        directory.mkdir(parents=True, exist_ok=True)
        _drop_dedicated_cover(product, directory)
        stored = f"cover_{uuid.uuid4().hex}{ext}"
        path = (
            directory / stored
        )  # SEC-PATH-OK: 'cover_' + uuid4().hex + an extension validated against the cover allowlist just above
        content = await upload.read()
        try:
            await asyncio.to_thread(path.write_bytes, content)
        except OSError as e:
            logger.error("Failed to save product cover image %s: %s", path, e)
            raise HTTPException(status_code=500, detail="Failed to save cover image") from e
        product.cover_image_filename = stored
        await db.flush()
        return {"status": "success", "filename": stored, "size": len(content)}

    try:
        pick = CoverPickRequest.model_validate(await request.json())
    except Exception as e:
        raise HTTPException(status_code=400, detail="Body must be JSON {filename} or a multipart file") from e
    safe_attachment_name(pick.filename)
    entry = attachment_entry(product, pick.filename)
    if entry is None or entry.get("category") != "pictures":
        raise HTTPException(status_code=400, detail="The cover must be a picture attachment of this product")
    _drop_dedicated_cover(product, directory)
    product.cover_image_filename = pick.filename
    await db.flush()
    return {"status": "success", "filename": pick.filename}


@router.get("/{product_id}/cover-image")
async def get_product_cover_image(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    _=RequireCameraStreamToken,
):
    """The effective cover — the explicit column, else the first picture."""
    product = await _get(db, product_id)
    name = effective_cover(product)
    if not name:
        raise HTTPException(status_code=404, detail="No cover image set")
    safe_attachment_name(name)
    path = product_attachments_dir(product_id) / name  # SEC-PATH-OK: guarded by safe_attachment_name just above
    if not path.exists():
        # Whatever named this file vanished with it. Heal BOTH shapes of the
        # cover rule — and RETURN the 404 rather than raise it: ``get_db`` rolls
        # the request back on anything that escapes the handler, so a raise would
        # undo the very heal it just performed. (The project cover route's twin
        # has exactly that bug.)
        logger.warning("Cover image file missing for product %s: %s", product_id, path)
        if product.cover_image_filename == name:
            product.cover_image_filename = None
        else:
            # The IMPLICIT cover: no column to clear, so the gallery entry that
            # elected itself is the thing that is wrong. Leaving it would make
            # `has_cover` keep promising a picture every request 404s on, and
            # would hide every later picture behind a tile that never loads.
            product.attachments = [
                a for a in (product.attachments or []) if not (isinstance(a, dict) and a.get("filename") == name)
            ]
        await db.flush()
        return JSONResponse(status_code=404, content={"detail": "Cover image file not found"})
    # ⚠️ ``no-cache`` — REVALIDATE, not "do not store". This URL is stable across
    # the cover being replaced, so an age-based cache shows the old picture after
    # an upload; ``private`` alone still let a browser reuse a heuristically
    # fresh copy, which is what a cache-busting query param on the frontend was
    # working around. ``private``: token-gated user data, never a shared cache's.
    return FileResponse(path, media_type=image_media_type(name), headers={"Cache-Control": "private, no-cache"})


@router.delete("/{product_id}/cover-image")
async def delete_product_cover_image(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Clears the explicit choice; the first-picture default resumes."""
    product = await _get(db, product_id)
    if product.cover_image_filename:
        _drop_dedicated_cover(product, product_attachments_dir(product_id))
        product.cover_image_filename = None
        await db.flush()
    return {"status": "success"}
