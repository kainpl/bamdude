"""Orders (projects): lines of products for a customer, figures from the archive.

Spec: docs/superpowers/specs/2026-09-02-projects-redesign-design.md.
Route handlers never commit — the get_db dependency does.
"""

import asyncio
import logging
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.auth import RequireCameraStreamToken, RequirePermission
from backend.app.core.config import settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.customer import Customer
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.product import Product, ProductPart
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine, ProjectProcurement
from backend.app.models.user import User
from backend.app.schemas.project import (
    PROJECT_PRIORITIES,
    PROJECT_STATUSES,
    BatchAddArchives,
    BatchAddQueueItems,
    PartFiguresOut,
    ProcurementOut,
    ProcurementUpdate,
    ProjectCreate,
    ProjectDuplicate,
    ProjectFiguresOut,
    ProjectLineCreate,
    ProjectLineResponse,
    ProjectLineUpdate,
    ProjectListResponse,
    ProjectResponse,
    ProjectUpdate,
    TimelineEvent,
)
from backend.app.services.order_metrics import attribute, load_order_context, procurement_figures, project_figures

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])


# ---------- response building ----------


async def _get_project(db: AsyncSession, project_id: int) -> Project:
    project = (
        await db.execute(
            select(Project)
            .options(selectinload(Project.lines), selectinload(Project.customer))
            .where(Project.id == project_id)
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def _response(db: AsyncSession, project_id: int) -> ProjectResponse:
    """The one response builder — every mutating handler returns through it.

    ⚠️ Lines are added and removed through ``Project.lines``, never with a bare
    ``db.add(ProjectLine(project_id=...))``. An eager loader does not overwrite
    a collection it finds already loaded, so a line filed straight into the
    table would be missing from the very answer that reports it.
    """
    await db.flush()
    ctx = await load_order_context(db, project_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Project not found")
    figs, other = attribute(ctx)
    project = ctx.project
    customer = await db.get(Customer, project.customer_id) if project.customer_id else None
    lines = [
        ProjectLineResponse(
            id=line.id,
            product_id=line.product_id,
            product_name=ctx.products_by_id[line.product_id].name if line.product_id in ctx.products_by_id else "?",
            quantity=line.quantity,
            material=line.material,
            color=line.color,
            note=line.note,
            sort_order=line.sort_order,
            units_printed=figs[line.id].units_printed,
            progress=figs[line.id].progress,
            parts=[
                PartFiguresOut(
                    part_id=p.part_id,
                    name=p.name,
                    qty_per_unit=p.qty_per_unit,
                    need=p.need,
                    usable=p.usable,
                    in_progress=p.in_progress,
                    remaining=p.remaining,
                    surplus=p.surplus,
                )
                for p in figs[line.id].parts
            ],
        )
        for line in ctx.lines
    ]
    pf = project_figures(ctx, figs, other)
    return ProjectResponse(
        id=project.id,
        name=project.name,
        customer_id=project.customer_id,
        customer_name=customer.name if customer else None,
        description=project.description,
        color=project.color,
        status=project.status,
        notes=project.notes,
        attachments=project.attachments,
        tags=project.tags,
        due_date=project.due_date,
        priority=project.priority,
        price=project.price,
        url=project.url,
        cover_image_filename=project.cover_image_filename,
        created_at=project.created_at,
        updated_at=project.updated_at,
        lines=lines,
        procurement=[
            ProcurementOut(part_id=p.part_id, name=p.name, need=p.need, acquired=p.acquired, remaining=p.remaining)
            for p in procurement_figures(ctx, figs)
        ],
        figures=ProjectFiguresOut(**pf.__dict__),
    )


# ---------- CRUD ----------


@router.get("", response_model=list[ProjectListResponse])
@router.get("/", response_model=list[ProjectListResponse])
async def list_projects(
    status: str | None = None,
    customer_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    query = (
        select(Project)
        .options(selectinload(Project.lines), selectinload(Project.customer))
        .order_by(Project.updated_at.desc())
    )
    if status:
        query = query.where(Project.status == status)
    if customer_id is not None:
        query = query.where(Project.customer_id == customer_id)
    projects = (await db.execute(query)).scalars().all()
    product_ids = {line.product_id for p in projects for line in p.lines}
    covers = (
        dict(
            (
                await db.execute(select(Product.id, Product.cover_image_filename).where(Product.id.in_(product_ids)))
            ).all()
        )
        if product_ids
        else {}
    )
    out: list[ProjectListResponse] = []
    for project in projects:
        # One order context per row. Accepted for now (spec: measured later) —
        # an order has tens of archives and a farm has tens of orders.
        ctx = await load_order_context(db, project.id)
        if ctx is None:  # deleted between the two statements; nothing to report
            continue
        figs, other = attribute(ctx)
        pf = project_figures(ctx, figs, other)
        out.append(
            ProjectListResponse(
                id=project.id,
                name=project.name,
                customer_id=project.customer_id,
                customer_name=project.customer.name if project.customer else None,
                color=project.color,
                status=project.status,
                due_date=project.due_date,
                priority=project.priority,
                price=project.price,
                tags=project.tags,
                cover_image_filename=project.cover_image_filename,
                created_at=project.created_at,
                lines_count=len(project.lines),
                ordered=pf.ordered,
                printed=pf.printed,
                progress=pf.progress,
                product_cover_filenames=[covers.get(line.product_id) for line in project.lines],
            )
        )
    return out


async def _check_customer(db: AsyncSession, customer_id: int | None) -> None:
    if customer_id is not None and await db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=404, detail="Customer not found")


async def _check_product(db: AsyncSession, product_id: int) -> Product:
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.post("", response_model=ProjectResponse)
@router.post("/", response_model=ProjectResponse)
async def create_project(
    data: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_CREATE),
):
    await _check_customer(db, data.customer_id)
    for line in data.lines:
        await _check_product(db, line.product_id)
    project = Project(**data.model_dump(exclude={"lines"}))
    # Appended BEFORE the flush: on a pending row the collection is created
    # empty without a query, and the cascade fills in ``project_id``. Touching
    # it after the flush would be a lazy load, which async SQLAlchemy refuses.
    for i, line in enumerate(data.lines):
        project.lines.append(ProjectLine(sort_order=i, **line.model_dump()))
    db.add(project)
    await db.flush()
    return await _response(db, project.id)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: int, db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.PROJECTS_READ)
):
    return await _response(db, project_id)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: int,
    data: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    project = await _get_project(db, project_id)
    if data.status is not None and data.status not in PROJECT_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")
    if data.priority is not None and data.priority not in PROJECT_PRIORITIES:
        raise HTTPException(status_code=400, detail="Invalid priority")
    if "customer_id" in data.model_fields_set:
        await _check_customer(db, data.customer_id)
    # Every field keys off model_fields_set: an explicit null CLEARS, an absent
    # field leaves the column alone (the tags/due_date/#2536 lesson, applied to all).
    for field_name in data.model_fields_set:
        setattr(project, field_name, getattr(data, field_name))
    return await _response(db, project.id)


@router.delete("/{project_id}")
async def delete_project(
    project_id: int, db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.PROJECTS_DELETE)
):
    """Archives and queue rows survive, unlinked (SET NULL done explicitly — SQLite enforces nothing)."""
    project = await _get_project(db, project_id)
    for model in (PrintArchive, PrintQueueItem, AutoQueueItem):
        await db.execute(
            update(model).where(model.project_id == project_id).values(project_id=None, project_line_id=None)
        )
    await db.delete(project)
    return {"message": "Project deleted"}


# ---------- lines ----------


@router.post("/{project_id}/lines", response_model=ProjectResponse)
async def add_line(
    project_id: int,
    data: ProjectLineCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    project = await _get_project(db, project_id)
    await _check_product(db, data.product_id)
    project.lines.append(
        ProjectLine(sort_order=max((ln.sort_order for ln in project.lines), default=-1) + 1, **data.model_dump())
    )
    return await _response(db, project.id)


async def _get_line(db: AsyncSession, project_id: int, line_id: int) -> ProjectLine:
    line = await db.get(ProjectLine, line_id)
    if line is None or line.project_id != project_id:
        raise HTTPException(status_code=404, detail="Order line not found")
    return line


@router.patch("/{project_id}/lines/{line_id}", response_model=ProjectResponse)
async def update_line(
    project_id: int,
    line_id: int,
    data: ProjectLineUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    line = await _get_line(db, project_id, line_id)
    for field_name in data.model_fields_set:
        setattr(line, field_name, getattr(data, field_name))
    return await _response(db, project_id)


@router.delete("/{project_id}/lines/{line_id}", response_model=ProjectResponse)
async def delete_line(
    project_id: int,
    line_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    line = await _get_line(db, project_id, line_id)
    # The prints stay, and stay in the order: only the line they were filed
    # under goes. Cleared BEFORE the delete, or PostgreSQL refuses the row.
    for model in (PrintArchive, PrintQueueItem, AutoQueueItem):
        await db.execute(update(model).where(model.project_line_id == line_id).values(project_line_id=None))
    project = await _get_project(db, project_id)
    project.lines.remove(line)  # delete-orphan turns this into the DELETE
    return await _response(db, project_id)


# ---------- procurement ----------


@router.patch("/{project_id}/procurement/{part_id}", response_model=ProjectResponse)
async def update_procurement(
    project_id: int,
    part_id: int,
    data: ProcurementUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    project = await _get_project(db, project_id)
    part = await db.get(ProductPart, part_id)
    if part is None or part.kind != "purchased" or part.product_id not in {ln.product_id for ln in project.lines}:
        raise HTTPException(status_code=404, detail="Purchased part not found in this order")
    row = await db.get(ProjectProcurement, {"project_id": project_id, "product_part_id": part_id})
    if row is None:
        db.add(
            ProjectProcurement(project_id=project_id, product_part_id=part_id, quantity_acquired=data.quantity_acquired)
        )
    else:
        row.quantity_acquired = data.quantity_acquired
    return await _response(db, project_id)


# ---------- archives & queue ----------


@router.get("/{project_id}/archives")
async def list_project_archives(
    project_id: int,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """List archives in a project."""
    # Verify project exists
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    # Eager-load the relationships that archive_to_response touches —
    # created_by.username otherwise triggers a lazy load against an
    # already-returned async session → MissingGreenlet crash.
    query = (
        select(PrintArchive)
        .options(selectinload(PrintArchive.project), selectinload(PrintArchive.created_by))
        .where(PrintArchive.project_id == project_id)
        .order_by(PrintArchive.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(query)
    archives = result.scalars().all()

    # Import the response converter from archives module
    from backend.app.api.routes.archives import archive_to_response

    return [archive_to_response(a) for a in archives]


@router.post("/{project_id}/add-archives")
async def add_archives_to_project(
    project_id: int,
    data: BatchAddArchives,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """File existing prints under this order, optionally under one of its lines."""
    await _get_project(db, project_id)
    if data.project_line_id is not None:
        await _get_line(db, project_id, data.project_line_id)
    updated = 0
    for archive_id in data.archive_ids:
        archive = await db.get(PrintArchive, archive_id)
        if archive:
            archive.project_id = project_id
            archive.project_line_id = data.project_line_id
            updated += 1
    return {"message": f"Added {updated} archives to project"}


@router.post("/{project_id}/remove-archives")
async def remove_archives_from_project(
    project_id: int,
    data: BatchAddArchives,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Unfile prints from this order — the line goes with the order, never alone."""
    updated = 0
    for archive_id in data.archive_ids:
        archive = (
            await db.execute(
                select(PrintArchive).where(PrintArchive.id == archive_id, PrintArchive.project_id == project_id)
            )
        ).scalar_one_or_none()
        if archive:
            archive.project_id = None
            archive.project_line_id = None
            updated += 1
    return {"message": f"Removed {updated} archives from project"}


@router.post("/{project_id}/add-queue")
async def add_queue_items_to_project(
    project_id: int,
    data: BatchAddQueueItems,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Batch add queue items to a project."""
    # Verify project exists
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    # Update queue items
    updated = 0
    for item_id in data.queue_item_ids:
        result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))
        item = result.scalar_one_or_none()
        if item:
            item.project_id = project_id
            updated += 1

    return {"message": f"Added {updated} queue items to project"}


# ---------- attachments ----------


def get_project_attachments_dir(project_id: int) -> Path:
    """Get the attachments directory for a project."""
    base_dir = Path(settings.archive_dir)
    return base_dir / "projects" / str(project_id) / "attachments"


# Allowed file extensions for attachments
ALLOWED_ATTACHMENT_EXTENSIONS = {
    # Images
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".bmp",
    ".ico",
    # Documents
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
    ".txt",
    ".rtf",
    ".csv",
    ".md",
    # 3D/CAD files
    ".stl",
    ".obj",
    ".3mf",
    ".step",
    ".stp",
    ".iges",
    ".igs",
    ".f3d",
    ".scad",
    # Archives
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    # Code/scripts (for Klipper macros, scripts, etc.)
    ".py",
    ".sh",
    ".cfg",
    ".conf",
    ".gcode",
    ".ini",
    # Other common formats
    ".json",
    ".xml",
    ".yaml",
    ".yml",
}


@router.post("/{project_id}/attachments")
async def upload_attachment(
    project_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Upload an attachment to a project."""
    logger.info("=== UPLOAD START: %s for project %s ===", file.filename, project_id)

    # Verify project exists
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate file extension
    original_name = file.filename or "unknown"
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{ext}' not supported. Allowed: images, PDFs, documents, STL, 3MF, archives.",
        )

    # Create attachments directory
    attachments_dir = get_project_attachments_dir(project_id)
    attachments_dir.mkdir(parents=True, exist_ok=True)

    # Generate unique filename
    unique_filename = f"{uuid.uuid4().hex}{ext}"
    file_path = (
        attachments_dir / unique_filename
    )  # SEC-PATH-OK: unique_filename = uuid4().hex + an extension validated against the attachment allowlist just above

    # Save file
    try:
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
        logger.info("=== FILE SAVED: %s, size: %s ===", file_path, len(content))
    except Exception as e:
        logger.error("Failed to save attachment: %s", e)
        raise HTTPException(status_code=500, detail="Failed to save attachment")

    # Update project attachments JSON
    attachments = list(project.attachments or [])
    new_attachment = {
        "filename": unique_filename,
        "original_name": original_name,
        "size": len(content),
        "uploaded_at": datetime.now().isoformat(),
    }
    attachments.append(new_attachment)

    # Simple ORM update
    project.attachments = attachments
    db.add(project)  # Explicitly add to session

    logger.info("=== BEFORE COMMIT: %s attachments ===", len(attachments))

    await db.flush()
    await db.commit()

    logger.info("=== AFTER COMMIT ===")

    # Verify by re-querying
    result = await db.execute(select(Project).where(Project.id == project_id))
    fresh_project = result.scalar_one()

    logger.info("=== VERIFIED: %s attachments ===", len(fresh_project.attachments or []))

    return {
        "status": "success",
        "filename": unique_filename,
        "original_name": original_name,
        "attachments": fresh_project.attachments,
    }


@router.get("/{project_id}/attachments/{filename}")
async def download_attachment(
    project_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """Download an attachment from a project."""
    # Validate filename to prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename or not filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Verify project exists
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Verify attachment exists in project
    attachments = project.attachments or []
    attachment = next((a for a in attachments if a.get("filename") == filename), None)
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    # Check file exists
    file_path = (
        get_project_attachments_dir(project_id) / filename
    )  # SEC-PATH-OK: filename is rejected for / \ .. and empty just above the join
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Attachment file not found")

    return FileResponse(
        file_path,
        filename=attachment.get("original_name", filename),
        media_type="application/octet-stream",
    )


@router.delete("/{project_id}/attachments/{filename}")
async def delete_attachment(
    project_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Delete an attachment from a project."""
    # Validate filename to prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename or not filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Verify project exists
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Find and remove attachment from list
    attachments = project.attachments or []
    attachment = next((a for a in attachments if a.get("filename") == filename), None)
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    # Remove from list
    attachments = [a for a in attachments if a.get("filename") != filename]
    project.attachments = attachments if attachments else None

    # Delete file
    file_path = (
        get_project_attachments_dir(project_id) / filename
    )  # SEC-PATH-OK: filename is rejected for / \ .. and empty just above the join
    if file_path.exists():
        try:
            os.remove(file_path)
        except Exception as e:
            logger.warning("Failed to delete attachment file: %s", e)

    await db.flush()
    await db.refresh(project)

    return {
        "status": "success",
        "message": "Attachment deleted",
        "attachments": project.attachments,
    }


# ============ B.2 (#1155) — Project cover image ============

# Cover-image upload accepts only common web-renderable image types.
# Subset of ALLOWED_ATTACHMENT_EXTENSIONS minus .svg/.ico because those
# don't render well as a card thumbnail.
COVER_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
COVER_IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@router.post("/{project_id}/cover-image")
async def upload_project_cover_image(
    project_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Upload (or replace) the project's cover image (#1155).

    Stored alongside other attachments but tracked via
    ``Project.cover_image_filename`` so swap/delete operations don't
    touch the attachments list. Replaces any existing cover image — the
    prior file is deleted on disk before the new one lands so a stuck
    filesystem reference can't accumulate orphaned images.
    """
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    original_name = file.filename or "cover"
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in COVER_IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Cover image must be one of {sorted(COVER_IMAGE_EXTENSIONS)}",
        )

    attachments_dir = get_project_attachments_dir(project_id)
    attachments_dir.mkdir(parents=True, exist_ok=True)

    # Remove the previous cover-image file from disk first so we don't
    # accumulate orphans when users repeatedly replace it. Best-effort:
    # a missing/locked file shouldn't block a successful replacement.
    if project.cover_image_filename:
        old_path = attachments_dir / project.cover_image_filename
        if old_path.exists():
            try:
                os.remove(old_path)
            except OSError as e:
                logger.warning("Failed to delete old cover image %s: %s", old_path, e)

    unique_filename = f"cover_{uuid.uuid4().hex}{ext}"
    file_path = (
        attachments_dir / unique_filename
    )  # SEC-PATH-OK: unique_filename = 'cover_' + uuid4().hex + an extension validated against the cover-image allowlist just above
    try:
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
    except OSError as e:
        logger.error("Failed to save cover image: %s", e)
        raise HTTPException(status_code=500, detail="Failed to save cover image") from e

    project.cover_image_filename = unique_filename
    db.add(project)
    await db.flush()

    return {
        "status": "success",
        "filename": unique_filename,
        "size": len(content),
    }


@router.get("/{project_id}/cover-image")
async def get_project_cover_image(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _=RequireCameraStreamToken,
):
    """Stream the project's cover image (#1155).

    Browsers can't attach ``Authorization: Bearer ...`` to ``<img src>``
    requests, so this route accepts the same ``?token=`` stream
    credential as ``/archives/{id}/thumbnail``. The frontend wraps URLs
    via ``withStreamToken``.
    """
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.cover_image_filename:
        raise HTTPException(status_code=404, detail="No cover image set")

    file_path = get_project_attachments_dir(project_id) / project.cover_image_filename
    if not file_path.exists():
        # DB references a file that vanished from disk — clear the
        # dangling reference so future GETs get a clean 404 instead of
        # repeatedly touching the filesystem.
        logger.warning("Cover image file missing for project %s: %s", project_id, file_path)
        project.cover_image_filename = None
        await db.flush()
        raise HTTPException(status_code=404, detail="Cover image file not found")

    ext = os.path.splitext(project.cover_image_filename)[1].lower()
    media_type = COVER_IMAGE_CONTENT_TYPES.get(ext, "application/octet-stream")
    return FileResponse(file_path, media_type=media_type)


@router.delete("/{project_id}/cover-image")
async def delete_project_cover_image(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Remove the project's cover image (#1155)."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.cover_image_filename:
        file_path = get_project_attachments_dir(project_id) / project.cover_image_filename
        if file_path.exists():
            try:
                os.remove(file_path)
            except OSError as e:
                logger.warning("Failed to delete cover image file %s: %s", file_path, e)
        project.cover_image_filename = None
        db.add(project)
        await db.flush()

    return {"status": "success"}


# ============ Timeline ============


# An archive exists for every physical print — queue-driven, auto-queued, direct
# and printer-started alike (see the archive-is-print-history invariant), and the
# dispatcher stamps ``project_id`` onto it on every path. So the archive is the
# timeline's source for anything that reached a printer, and the two queue tables
# are asked only about work that has NOT reached one yet.
#
# The previous version took "print started" from ``PrintQueueItem`` instead, and
# turned archives into events only for ``completed`` / ``failed``. A print
# dispatched straight to a printer therefore appeared nowhere at all — no queue
# row to read, and a ``printing`` archive it ignored — while cancelled prints
# were invisible in every case.
_ARCHIVE_EVENT_BY_STATUS = {
    "printing": "print_started",
    "completed": "print_completed",
    "failed": "print_failed",
    "aborted": "print_cancelled",
    "cancelled": "print_cancelled",
    "stopped": "print_cancelled",
}

# English, and deliberately so: ``title`` is the API's own wording for callers
# that are not our frontend, which renders each event from ``event_type`` in the
# user's language and never shows these.
_EVENT_TITLES = {
    "print_started": "Print started",
    "print_completed": "Print completed",
    "print_failed": "Print failed",
    "print_cancelled": "Print cancelled",
    "queued": "Added to queue",
    "auto_queued": "Added to auto-queue",
    "project_created": "Project created",
}


def _archive_event_timestamp(archive: PrintArchive) -> datetime:
    """When the event being described actually happened.

    A finished print is placed at its end, a running one at its start. Falls back
    to ``created_at``, which every row has.
    """
    if _ARCHIVE_EVENT_BY_STATUS.get(archive.status) == "print_started":
        return archive.started_at or archive.created_at
    return archive.completed_at or archive.started_at or archive.created_at


def _queue_display_name(item) -> str:
    """Best-effort name for a queue row, which has no name of its own.

    Works for both queue tables: each references an archive and/or a library
    file, and neither carries a ``print_name`` column — reading one off the row
    itself is what used to 500 this endpoint.
    """
    return (
        (item.archive.print_name if item.archive else None)
        or (item.archive.filename if item.archive else None)
        or (item.library_file.filename if item.library_file else None)
        or "(unnamed queue item)"
    )


@router.get("/{project_id}/timeline", response_model=list[TimelineEvent])
async def get_project_timeline(
    project_id: int,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """Everything that happened to a project, newest first.

    Prints come from archives (running, finished, failed and cancelled alike);
    the two queue tables contribute only work still waiting, so nothing appears
    twice — a queue item that has been dispatched is no longer ``pending`` and
    its archive speaks for it from then on.

    Statuses are filtered **in SQL** rather than after the fetch. Filtering
    afterwards spent the limit on rows that were then discarded, so a project
    whose twenty most recent archives were all cancelled showed an empty
    timeline. Each source is ordered by the same value used as the event's
    timestamp, so taking the newest ``limit`` from each and cutting the merged
    list to ``limit`` yields exactly the newest ``limit`` overall.
    """
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    events: list[TimelineEvent] = []

    # Prints, in every state that says something happened.
    archive_order = func.coalesce(PrintArchive.completed_at, PrintArchive.started_at, PrintArchive.created_at)
    archives = (
        (
            await db.execute(
                select(PrintArchive)
                .where(PrintArchive.project_id == project_id)
                .where(PrintArchive.deleted_at.is_(None))
                .where(PrintArchive.status.in_(list(_ARCHIVE_EVENT_BY_STATUS)))
                .order_by(archive_order.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    archive_ids = {archive.id for archive in archives}

    for archive in archives:
        metadata: dict = {"archive_id": archive.id, "status": archive.status}
        if archive.print_time_seconds:
            metadata["print_time_hours"] = round(archive.print_time_seconds / 3600, 2)
        if archive.filament_used_grams:
            metadata["filament_grams"] = round(archive.filament_used_grams, 1)
        if archive.failure_reason:
            metadata["failure_reason"] = archive.failure_reason
        events.append(
            TimelineEvent(
                event_type=_ARCHIVE_EVENT_BY_STATUS[archive.status],
                timestamp=_archive_event_timestamp(archive),
                title=_EVENT_TITLES[_ARCHIVE_EVENT_BY_STATUS[archive.status]],
                description=archive.print_name or archive.filename,
                metadata=metadata,
            )
        )

    # Per-printer queue: only what is still waiting. A dispatched item has left
    # 'pending', and ``archive_id`` guards the overlap the status cannot — a
    # pending row that already points at one of the archives above (a reprint
    # queued from it) would otherwise be listed twice, once as work and once as
    # the print it produced.
    queued_items = (
        (
            await db.execute(
                select(PrintQueueItem)
                .options(selectinload(PrintQueueItem.archive), selectinload(PrintQueueItem.library_file))
                .where(PrintQueueItem.project_id == project_id)
                .where(PrintQueueItem.status == "pending")
                .order_by(PrintQueueItem.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    for item in queued_items:
        if item.archive_id and item.archive_id in archive_ids:
            continue
        events.append(
            TimelineEvent(
                event_type="queued",
                timestamp=item.created_at,
                title=_EVENT_TITLES["queued"],
                description=_queue_display_name(item),
                metadata={"queue_item_id": item.id},
            )
        )

    # Auto-queue: work not yet routed to any printer. Once routed the row turns
    # 'assigned' and a per-printer item takes over, so the two tables cannot both
    # claim the same job; ``assigned_to_item_id`` is belt and braces for a row
    # routed between the two queries.
    auto_items = (
        (
            await db.execute(
                select(AutoQueueItem)
                .options(selectinload(AutoQueueItem.archive), selectinload(AutoQueueItem.library_file))
                .where(AutoQueueItem.project_id == project_id)
                .where(AutoQueueItem.status == "pending")
                .where(AutoQueueItem.assigned_to_item_id.is_(None))
                .order_by(AutoQueueItem.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    for item in auto_items:
        if item.archive_id and item.archive_id in archive_ids:
            continue
        events.append(
            TimelineEvent(
                event_type="auto_queued",
                timestamp=item.created_at,
                title=_EVENT_TITLES["auto_queued"],
                description=_queue_display_name(item),
                metadata={"auto_queue_item_id": item.id, "target_model": item.target_model},
            )
        )

    events.append(
        TimelineEvent(
            event_type="project_created",
            timestamp=project.created_at,
            title=_EVENT_TITLES["project_created"],
            description=project.name,
        )
    )

    events.sort(key=lambda e: e.timestamp, reverse=True)

    return events[:limit]


# ---------- duplicate ----------


def _duplicate_name(base: str, taken: set[str]) -> str:
    """``"X" -> "X (Copy)"``, then ``"X (Copy 2)"`` and so on.

    Project names carry no unique constraint, so this is politeness rather
    than correctness — three rows all called "Voron (Copy)" are legal and
    unusable.
    """
    candidate = f"{base} (Copy)"
    if candidate not in taken:
        return candidate
    n = 2
    while f"{base} (Copy {n})" in taken:
        n += 1
    return f"{base} (Copy {n})"


async def _copy_attachment_files(source_id: int, new_id: int) -> bool:
    """Copy ``projects/<id>/attachments`` across. True when the copy stands.

    ``attachments`` and ``cover_image_filename`` name files inside a
    per-project directory, so copying the columns alone would give the new
    project a file list and a cover that resolve to nothing — and would tie
    its images to the source's lifetime, where deleting the source takes them.
    """
    src = get_project_attachments_dir(source_id)
    if not src.is_dir():
        return True  # nothing to carry; the columns will be empty anyway
    try:
        await asyncio.to_thread(shutil.copytree, src, get_project_attachments_dir(new_id), dirs_exist_ok=True)
        return True
    except OSError as e:
        logger.warning("Project %s: attachments could not be copied from %s: %s", new_id, source_id, e)
        return False


@router.post("/{project_id}/duplicate", response_model=ProjectResponse)
async def duplicate_project(
    project_id: int,
    data: ProjectDuplicate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_CREATE),
):
    """A reorder: lines, customer, notes, attachments come across; history never does; status is active."""
    source = await _get_project(db, project_id)
    taken = set((await db.execute(select(Project.name))).scalars().all())
    copy = Project(
        name=data.name or _duplicate_name(source.name, taken),
        customer_id=source.customer_id,
        description=source.description,
        color=source.color,
        status="active",
        notes=source.notes,
        tags=source.tags,
        due_date=source.due_date,
        priority=source.priority,
        price=source.price,
        url=source.url,
    )
    for line in source.lines:
        copy.lines.append(
            ProjectLine(
                product_id=line.product_id,
                quantity=line.quantity,
                material=line.material,
                color=line.color,
                note=line.note,
                sort_order=line.sort_order,
            )
        )
    db.add(copy)
    await db.flush()
    if source.attachments or source.cover_image_filename:
        if await _copy_attachment_files(source.id, copy.id):
            copy.attachments = source.attachments
            copy.cover_image_filename = source.cover_image_filename
    return await _response(db, copy.id)
