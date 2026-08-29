import asyncio
import copy as copy_module
import hashlib
import io
import json
import logging
import os
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.api.routes.library import get_library_dir
from backend.app.core.auth import RequireCameraStreamToken, RequirePermission
from backend.app.core.config import settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.library_project_links import library_file_projects, library_folder_projects
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.project import Project
from backend.app.models.project_bom import ProjectBOMItem
from backend.app.models.project_part import ProjectPart
from backend.app.models.project_print_plan import ProjectPrintPlanItem
from backend.app.models.user import User
from backend.app.schemas.project import (
    ArchivePreview,
    BatchAddArchives,
    BatchAddQueueItems,
    BOMItemCreate,
    BOMItemResponse,
    BOMItemUpdate,
    PrintPlanItemResponse,
    PrintPlanItemUpdate,
    PrintPlanReorderRequest,
    PrintPlanResponse,
    ProjectChildPreview,
    ProjectCreate,
    ProjectDuplicate,
    ProjectImport,
    ProjectListResponse,
    ProjectPartRow,
    ProjectPartsResponse,
    ProjectPartsUpdate,
    ProjectResponse,
    ProjectStats,
    ProjectUpdate,
    TimelineEvent,
)
from backend.app.services.library_helpers import detect_file_type, sync_system_tags
from backend.app.services.library_ingest import find_reusable_row
from backend.app.utils.http import build_content_disposition
from backend.app.utils.safe_path import safe_join_under

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])


async def subtree_project_ids(db: AsyncSession, root_id: int) -> list[int]:
    """``root_id`` and every project beneath it, breadth-first.

    ⚠️ Carries a seen-set even though :func:`would_create_project_cycle` refuses
    to build one: a database written before that guard existed can already
    contain a loop, and a roll-up that hangs is worse than one that is wrong.
    """
    found = [root_id]
    seen = {root_id}
    frontier = [root_id]
    while frontier:
        rows = await db.execute(select(Project.id).where(Project.parent_id.in_(frontier)))
        frontier = [pid for pid in rows.scalars().all() if pid not in seen]
        seen.update(frontier)
        found.extend(frontier)
    return found


async def would_create_project_cycle(db: AsyncSession, project_id: int, new_parent_id: int) -> bool:
    """Whether making ``new_parent_id`` the parent of ``project_id`` closes a loop.

    ⚠️ Refusing only ``parent == self`` is not enough: A→B then B→A is two calls
    apart, and the result is a tree with no root to roll figures up to. Walks
    upward from the proposed parent, which is at most as deep as the tree.
    """
    if new_parent_id == project_id:
        return True
    seen: set[int] = set()
    current: int | None = new_parent_id
    while current is not None and current not in seen:
        seen.add(current)
        if current == project_id:
            return True
        row = await db.execute(select(Project.parent_id).where(Project.id == current))
        current = row.scalar_one_or_none()
    return False


async def compute_project_stats(
    db: AsyncSession,
    project_id: int | list[int],
    target_count: int | None = None,
    target_parts_count: int | None = None,
) -> ProjectStats:
    """Compute statistics for one project, or for a whole set of them.

    ⚠️ A single id and a subtree go through **this** function rather than a
    second copy of the SQL: a master project's roll-up and its own card must
    agree, and two queries that answer the same question eventually stop
    agreeing (upstream #1264 consolidated theirs for the same reason).

    ``target_count`` / ``target_parts_count`` are the caller's — for a roll-up
    that means every target in the tree added together, so the percentage is
    measured against what the whole tree set out to do.
    """
    project_ids = [project_id] if isinstance(project_id, int) else list(project_id)
    in_scope = PrintArchive.project_id.in_(project_ids)
    # Count total archives (distinct print jobs)
    total_result = await db.execute(
        select(func.count(PrintArchive.id)).where(in_scope, PrintArchive.deleted_at.is_(None))
    )
    total_archives = total_result.scalar() or 0

    # Sum total items (using quantity field)
    total_items_result = await db.execute(
        select(func.coalesce(func.sum(PrintArchive.quantity), 0)).where(in_scope, PrintArchive.deleted_at.is_(None))
    )
    total_items = total_items_result.scalar() or 0

    # Count failed archives (number of print jobs) - includes all failure states
    failed_result = await db.execute(
        select(func.count(PrintArchive.id)).where(
            in_scope,
            PrintArchive.deleted_at.is_(None),
            PrintArchive.status.in_(["failed", "aborted", "cancelled", "stopped"]),
        )
    )
    failed_prints = failed_result.scalar() or 0

    # Sum print time, filament, and energy
    sums_result = await db.execute(
        select(
            # Real time where it is known, the slicer's estimate otherwise.
            # ``actual_time_seconds`` is filled for completed prints (m107); a
            # running or failed one has only the estimate. Summing the estimate
            # throughout meant the card reported what the slicer predicted, not
            # what the farm spent.
            func.coalesce(
                func.sum(func.coalesce(PrintArchive.actual_time_seconds, PrintArchive.print_time_seconds)), 0
            ).label("total_time"),
            func.coalesce(func.sum(PrintArchive.filament_used_grams), 0).label("total_filament"),
            func.coalesce(func.sum(PrintArchive.cost), 0).label("total_filament_cost"),
            func.coalesce(func.sum(PrintArchive.energy_kwh), 0).label("total_energy"),
            func.coalesce(func.sum(PrintArchive.energy_cost), 0).label("total_energy_cost"),
        ).where(in_scope, PrintArchive.deleted_at.is_(None))
    )
    sums = sums_result.first()

    # Count queued items
    queued_result = await db.execute(
        select(func.count(PrintQueueItem.id)).where(
            PrintQueueItem.project_id.in_(project_ids), PrintQueueItem.status == "pending"
        )
    )
    queued_prints = queued_result.scalar() or 0

    # Count in-progress prints from the ARCHIVE, not from the queue. Every
    # physical print has an archive stamped with the project; a queue row exists
    # only for work that went through a queue, so counting those missed prints
    # dispatched straight to a printer or started from its screen — the tile read
    # "0 in progress" with a machine visibly running the project's job.
    in_progress_result = await db.execute(
        select(func.count(PrintArchive.id)).where(
            in_scope,
            PrintArchive.deleted_at.is_(None),
            PrintArchive.status == "printing",
        )
    )
    in_progress_prints = in_progress_result.scalar() or 0

    # Parts actually produced: quantities of completed prints, less the ones
    # recorded as scrap. This is the figure the parts target is measured
    # against, and a project that needs 40 usable parts is not finished because
    # 40 came off the plate and three went in the bin. Only here — the archive's
    # own ``quantity`` and the global statistics keep meaning "what was
    # printed"; this is the project's question, not theirs.
    completed_items_result = await db.execute(
        select(
            func.coalesce(func.sum(PrintArchive.quantity), 0).label("printed"),
            func.coalesce(func.sum(PrintArchive.defective_count), 0).label("defective"),
        ).where(
            in_scope,
            PrintArchive.deleted_at.is_(None),
            PrintArchive.status == "completed",
        )
    )
    completed_row = completed_items_result.first()
    defective_items = int(completed_row.defective or 0)
    completed_items = max(0, int(completed_row.printed or 0) - defective_items)

    # Calculate progress for plates (target_count vs total_archives)
    progress_percent = None
    remaining_prints = None
    if target_count and target_count > 0:
        progress_percent = round((total_archives / target_count) * 100, 1)
        remaining_prints = max(0, target_count - total_archives)

    # Calculate progress for parts (target_parts_count vs completed_items)
    parts_progress_percent = None
    remaining_parts = None
    if target_parts_count and target_parts_count > 0:
        parts_progress_percent = round((completed_items / target_parts_count) * 100, 1)
        remaining_parts = max(0, target_parts_count - completed_items)

    # BOM stats
    bom_result = await db.execute(
        select(
            func.count(ProjectBOMItem.id).label("total"),
            func.sum(case((ProjectBOMItem.quantity_acquired >= ProjectBOMItem.quantity_needed, 1), else_=0)).label(
                "completed"
            ),
            func.coalesce(func.sum(ProjectBOMItem.unit_price * ProjectBOMItem.quantity_needed), 0).label("bom_cost"),
        ).where(ProjectBOMItem.project_id.in_(project_ids))
    )
    bom_stats = bom_result.first()

    return ProjectStats(
        total_archives=total_archives,
        total_items=int(total_items),
        completed_prints=completed_items,  # usable parts: completed quantities less scrap
        defective_parts=defective_items,
        failed_prints=int(failed_prints),
        queued_prints=queued_prints,
        in_progress_prints=in_progress_prints,
        total_print_time_hours=round((sums.total_time or 0) / 3600, 2),
        total_filament_grams=round(sums.total_filament or 0, 2),
        progress_percent=progress_percent,
        parts_progress_percent=parts_progress_percent,
        estimated_cost=round((sums.total_filament_cost or 0), 2),
        total_energy_kwh=round((sums.total_energy or 0), 3),
        total_energy_cost=round((sums.total_energy_cost or 0), 3),
        remaining_prints=remaining_prints,
        remaining_parts=remaining_parts,
        bom_total_items=bom_stats.total or 0,
        bom_completed_items=int(bom_stats.completed or 0),
        bom_cost=round(float(bom_stats.bom_cost or 0), 2),
    )


@router.get("", response_model=list[ProjectListResponse])
@router.get("/", response_model=list[ProjectListResponse])
async def list_projects(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """List all projects with basic stats.

    Always excludes templates (``is_template=True``) - templates are served by
    the dedicated ``/templates`` endpoint.
    """
    query = select(Project).where(Project.is_template.is_(False))
    if status:
        query = query.where(Project.status == status)
    query = query.order_by(Project.updated_at.desc())

    result = await db.execute(query)
    projects = result.scalars().all()

    # Compute quick stats for each project
    response = []
    for project in projects:
        # Get archive count (number of print jobs)
        archive_count_result = await db.execute(
            select(func.count(PrintArchive.id)).where(
                PrintArchive.project_id == project.id, PrintArchive.deleted_at.is_(None)
            )
        )
        archive_count = archive_count_result.scalar() or 0

        # Get total items (sum of quantities)
        total_items_result = await db.execute(
            select(func.coalesce(func.sum(PrintArchive.quantity), 0)).where(
                PrintArchive.project_id == project.id, PrintArchive.deleted_at.is_(None)
            )
        )
        total_items = int(total_items_result.scalar() or 0)

        # Get queue count
        queue_count_result = await db.execute(
            select(func.count(PrintQueueItem.id)).where(
                PrintQueueItem.project_id == project.id,
                PrintQueueItem.status.in_(["pending", "printing"]),
            )
        )
        queue_count = queue_count_result.scalar() or 0

        # Usable parts from completed prints — scrap subtracted, matching the
        # project page. See compute_project_stats for why only here.
        completed_result = await db.execute(
            select(
                func.coalesce(func.sum(PrintArchive.quantity), 0).label("printed"),
                func.coalesce(func.sum(PrintArchive.defective_count), 0).label("defective"),
            ).where(
                PrintArchive.project_id == project.id,
                PrintArchive.deleted_at.is_(None),
                PrintArchive.status == "completed",
            )
        )
        completed_row = completed_result.first()
        defective_count = int(completed_row.defective or 0)
        completed_count = max(0, int(completed_row.printed or 0) - defective_count)

        # Sum failed parts (quantities) - includes all failure states
        failed_result = await db.execute(
            select(func.coalesce(func.sum(PrintArchive.quantity), 0)).where(
                PrintArchive.project_id == project.id,
                PrintArchive.deleted_at.is_(None),
                PrintArchive.status.in_(["failed", "aborted", "cancelled", "stopped"]),
            )
        )
        failed_count = int(failed_result.scalar() or 0)

        # Plates progress: archive_count / target_count
        progress_percent = None
        if project.target_count and project.target_count > 0:
            progress_percent = round((archive_count / project.target_count) * 100, 1)

        # Get archive previews (up to 6 most recent)
        archives_result = await db.execute(
            select(PrintArchive)
            .where(PrintArchive.project_id == project.id, PrintArchive.deleted_at.is_(None))
            .order_by(PrintArchive.created_at.desc())
            .limit(6)
        )
        archives = archives_result.scalars().all()
        archive_previews = [
            ArchivePreview(
                id=a.id,
                print_name=a.print_name,
                thumbnail_path=a.thumbnail_path,
                status=a.status,
                filament_type=a.filament_type,
                filament_color=a.filament_color,
            )
            for a in archives
        ]

        response.append(
            ProjectListResponse(
                id=project.id,
                name=project.name,
                description=project.description,
                color=project.color,
                status=project.status,
                parent_id=project.parent_id,
                is_template=project.is_template,
                target_count=project.target_count,
                target_parts_count=project.target_parts_count,
                budget=project.budget,
                created_at=project.created_at,
                tags=project.tags,
                due_date=project.due_date,
                priority=project.priority,
                archive_count=archive_count,
                total_items=total_items,
                completed_count=completed_count,
                defective_count=defective_count,
                failed_count=failed_count,
                queue_count=queue_count,
                progress_percent=progress_percent,
                archives=archive_previews,
                url=project.url,
                cover_image_filename=project.cover_image_filename,
            )
        )

    return response


@router.post("/", response_model=ProjectResponse)
async def create_project(
    data: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_CREATE),
):
    """Create a new project."""
    # Verify parent exists if specified
    parent_name = None
    if data.parent_id:
        parent_result = await db.execute(select(Project).where(Project.id == data.parent_id))
        parent = parent_result.scalar_one_or_none()
        if not parent:
            raise HTTPException(status_code=400, detail="Parent project not found")
        parent_name = parent.name

    project = Project(
        name=data.name,
        description=data.description,
        color=data.color,
        target_count=data.target_count,
        target_parts_count=data.target_parts_count,
        notes=data.notes,
        tags=data.tags,
        due_date=data.due_date,
        priority=data.priority,
        budget=data.budget,
        parent_id=data.parent_id,
        url=data.url,
    )
    db.add(project)
    await db.flush()
    await db.refresh(project)

    stats = await compute_project_stats(db, project.id, project.target_count, project.target_parts_count)

    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        color=project.color,
        status=project.status,
        target_count=project.target_count,
        target_parts_count=project.target_parts_count,
        notes=project.notes,
        attachments=project.attachments,
        url=project.url,
        cover_image_filename=project.cover_image_filename,
        tags=project.tags,
        due_date=project.due_date,
        priority=project.priority,
        budget=project.budget,
        is_template=project.is_template,
        template_source_id=project.template_source_id,
        parent_id=project.parent_id,
        parent_name=parent_name,
        children=[],
        created_at=project.created_at,
        updated_at=project.updated_at,
        stats=stats,
    )


# ============ Phase 8: Template Endpoints (Static routes BEFORE dynamic {project_id}) ============


@router.get("/templates", response_model=list[ProjectListResponse])
@router.get("/templates/", response_model=list[ProjectListResponse])
async def list_templates(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """List all project templates."""
    result = await db.execute(select(Project).where(Project.is_template.is_(True)).order_by(Project.name))
    templates = result.scalars().all()

    response = []
    for project in templates:
        # Get archive count
        archive_count_result = await db.execute(
            select(func.count(PrintArchive.id)).where(PrintArchive.project_id == project.id)
        )
        archive_count = archive_count_result.scalar() or 0

        response.append(
            ProjectListResponse(
                id=project.id,
                name=project.name,
                description=project.description,
                color=project.color,
                status=project.status,
                parent_id=project.parent_id,
                is_template=project.is_template,
                target_count=project.target_count,
                target_parts_count=project.target_parts_count,
                budget=project.budget,
                created_at=project.created_at,
                tags=project.tags,
                due_date=project.due_date,
                priority=project.priority,
                archive_count=archive_count,
                queue_count=0,
                progress_percent=None,
                archives=[],
                url=project.url,
                cover_image_filename=project.cover_image_filename,
            )
        )

    return response


@router.post("/from-template/{template_id}", response_model=ProjectResponse)
async def create_project_from_template(
    template_id: int,
    name: str = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_CREATE),
):
    """Create a new project from a template."""
    result = await db.execute(select(Project).where(Project.id == template_id))
    template = result.scalar_one_or_none()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    if not template.is_template:
        raise HTTPException(status_code=400, detail="Project is not a template")

    # Create new project
    project = Project(
        name=name or template.name.replace(" (Template)", ""),
        description=template.description,
        color=template.color,
        target_count=template.target_count,
        target_parts_count=template.target_parts_count,
        notes=template.notes,
        tags=template.tags,
        priority=template.priority,
        budget=template.budget,
        is_template=False,
        template_source_id=template.id,
    )
    db.add(project)
    await db.flush()

    # Copy BOM items
    bom_result = await db.execute(select(ProjectBOMItem).where(ProjectBOMItem.project_id == template_id))
    bom_items = bom_result.scalars().all()

    for item in bom_items:
        new_item = ProjectBOMItem(
            project_id=project.id,
            name=item.name,
            quantity_needed=item.quantity_needed,
            quantity_acquired=0,
            unit_price=item.unit_price,
            sourcing_url=item.sourcing_url,
            stl_filename=item.stl_filename,
            remarks=item.remarks,
            sort_order=item.sort_order,
        )
        db.add(new_item)

    await db.flush()
    await db.refresh(project)

    stats = await compute_project_stats(db, project.id, project.target_count, project.target_parts_count)

    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        color=project.color,
        status=project.status,
        target_count=project.target_count,
        target_parts_count=project.target_parts_count,
        notes=project.notes,
        attachments=project.attachments,
        url=project.url,
        cover_image_filename=project.cover_image_filename,
        tags=project.tags,
        due_date=project.due_date,
        priority=project.priority,
        budget=project.budget,
        is_template=project.is_template,
        template_source_id=project.template_source_id,
        parent_id=project.parent_id,
        parent_name=None,
        children=[],
        created_at=project.created_at,
        updated_at=project.updated_at,
        stats=stats,
    )


# ============ Dynamic {project_id} Routes ============


async def get_child_previews(db: AsyncSession, parent_id: int) -> list[ProjectChildPreview]:
    """Get preview info for child projects."""
    result = await db.execute(select(Project).where(Project.parent_id == parent_id).order_by(Project.name))
    children = result.scalars().all()

    previews = []
    for child in children:
        # Get completed count for progress (sum of quantities)
        completed_result = await db.execute(
            select(func.coalesce(func.sum(PrintArchive.quantity), 0)).where(
                PrintArchive.project_id == child.id,
                PrintArchive.status == "completed",
            )
        )
        completed_count = completed_result.scalar() or 0
        progress = None
        if child.target_count and child.target_count > 0:
            progress = round((int(completed_count) / child.target_count) * 100, 1)

        previews.append(
            ProjectChildPreview(
                id=child.id,
                name=child.name,
                color=child.color,
                status=child.status,
                progress_percent=progress,
            )
        )
    return previews


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """Get a project by ID with detailed stats."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Get parent name
    parent_name = None
    if project.parent_id:
        parent_result = await db.execute(select(Project.name).where(Project.id == project.parent_id))
        parent_name = parent_result.scalar()

    # Get children
    children = await get_child_previews(db, project.id)

    stats = await compute_project_stats(db, project.id, project.target_count, project.target_parts_count)

    # Roll the whole tree up, targets included: a master project's progress is
    # measured against what the tree set out to do, not against its own plate
    # count. Skipped entirely for a project with no children — there would be
    # nothing to add, and a duplicate card saying the same numbers reads as a
    # bug.
    rollup_stats = None
    if children:
        subtree = await subtree_project_ids(db, project.id)
        targets = await db.execute(
            select(
                func.coalesce(func.sum(Project.target_count), 0),
                func.coalesce(func.sum(Project.target_parts_count), 0),
            ).where(Project.id.in_(subtree))
        )
        tree_target, tree_parts_target = targets.first()
        rollup_stats = await compute_project_stats(db, subtree, tree_target or None, tree_parts_target or None)

    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        color=project.color,
        status=project.status,
        target_count=project.target_count,
        target_parts_count=project.target_parts_count,
        notes=project.notes,
        attachments=project.attachments,
        url=project.url,
        cover_image_filename=project.cover_image_filename,
        tags=project.tags,
        due_date=project.due_date,
        priority=project.priority,
        budget=project.budget,
        is_template=project.is_template,
        template_source_id=project.template_source_id,
        parent_id=project.parent_id,
        parent_name=parent_name,
        children=children,
        created_at=project.created_at,
        updated_at=project.updated_at,
        stats=stats,
        rollup_stats=rollup_stats,
    )


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: int,
    data: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Update a project."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Update fields if provided
    if data.name is not None:
        project.name = data.name
    if data.description is not None:
        project.description = data.description
    if data.color is not None:
        project.color = data.color
    if data.status is not None:
        if data.status not in ["active", "completed", "archived"]:
            raise HTTPException(status_code=400, detail="Invalid status")
        project.status = data.status
    # Keyed off model_fields_set, like tags / budget / url below: an explicit
    # null has to clear the target, and ``is not None`` made emptying the field
    # a no-op that silently kept the old number. Zero is a real value here — it
    # means "don't measure this project in plates (or parts)", and that progress
    # bar is then hidden rather than pinned at an impossible percentage.
    if "target_count" in data.model_fields_set:
        project.target_count = data.target_count
    if "target_parts_count" in data.model_fields_set:
        project.target_parts_count = data.target_parts_count
    if data.notes is not None:
        project.notes = data.notes
    if "tags" in data.model_fields_set:
        # Explicit null clears, same as budget/url below — an emptied tags field
        # used to go out as undefined and silently revert (upstream #2536).
        project.tags = data.tags
    if "due_date" in data.model_fields_set:
        project.due_date = data.due_date
    if data.priority is not None:
        if data.priority not in ["low", "normal", "high", "urgent"]:
            raise HTTPException(status_code=400, detail="Invalid priority")
        project.priority = data.priority
    if "budget" in data.model_fields_set:
        project.budget = data.budget
    if "url" in data.model_fields_set:
        # Allow explicit clear via null. The validator already rejected
        # non-http(s) inputs, so anything reaching here is safe to store.
        project.url = data.url
    if data.parent_id is not None:
        if data.parent_id != 0:  # 0 means remove parent
            parent_result = await db.execute(select(Project).where(Project.id == data.parent_id))
            if not parent_result.scalar_one_or_none():
                raise HTTPException(status_code=400, detail="Parent project not found")
            # ⚠️ The whole chain, not just "is this me". A→B followed by B→A is
            # two calls apart and leaves a tree with no root to roll figures up
            # to — which the stats walk then has to defend itself against.
            if await would_create_project_cycle(db, project_id, data.parent_id):
                raise HTTPException(status_code=400, detail="A project cannot be nested inside itself")
            project.parent_id = data.parent_id
        else:
            project.parent_id = None

    await db.flush()
    await db.refresh(project)

    # Get parent name
    parent_name = None
    if project.parent_id:
        parent_result = await db.execute(select(Project.name).where(Project.id == project.parent_id))
        parent_name = parent_result.scalar()

    # Get children
    children = await get_child_previews(db, project.id)

    stats = await compute_project_stats(db, project.id, project.target_count, project.target_parts_count)

    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        color=project.color,
        status=project.status,
        target_count=project.target_count,
        target_parts_count=project.target_parts_count,
        notes=project.notes,
        attachments=project.attachments,
        url=project.url,
        cover_image_filename=project.cover_image_filename,
        tags=project.tags,
        due_date=project.due_date,
        priority=project.priority,
        budget=project.budget,
        is_template=project.is_template,
        template_source_id=project.template_source_id,
        parent_id=project.parent_id,
        parent_name=parent_name,
        children=children,
        created_at=project.created_at,
        updated_at=project.updated_at,
        stats=stats,
    )


@router.delete("/{project_id}")
async def delete_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_DELETE),
):
    """Delete a project. Archives and queue items will have project_id set to NULL."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # ⚠️ Children move UP to this project's own parent, not out to the top
    # level. The FK carries no ``ondelete``, so SQLAlchemy would nullify it and
    # a mid-tree project's sub-projects would silently leave the tree they
    # belong to — losing the grouping that is the point of nesting them.
    await db.execute(update(Project).where(Project.parent_id == project_id).values(parent_id=project.parent_id))

    await db.delete(project)

    return {"message": "Project deleted"}


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
    """Batch add archives to a project."""
    # Verify project exists
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    # Update archives
    updated = 0
    for archive_id in data.archive_ids:
        result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
        archive = result.scalar_one_or_none()
        if archive:
            archive.project_id = project_id
            updated += 1

    return {"message": f"Added {updated} archives to project"}


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


@router.post("/{project_id}/remove-archives")
async def remove_archives_from_project(
    project_id: int,
    data: BatchAddArchives,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Remove archives from a project (sets project_id to NULL)."""
    updated = 0
    for archive_id in data.archive_ids:
        result = await db.execute(
            select(PrintArchive).where(
                PrintArchive.id == archive_id,
                PrintArchive.project_id == project_id,
            )
        )
        archive = result.scalar_one_or_none()
        if archive:
            archive.project_id = None
            updated += 1

    return {"message": f"Removed {updated} archives from project"}


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


# ============ Phase 7: BOM Endpoints ============


@router.get("/{project_id}/bom", response_model=list[BOMItemResponse])
async def list_bom_items(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """List all BOM items for a project."""
    # Verify project exists
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    # Get BOM items
    result = await db.execute(
        select(ProjectBOMItem)
        .where(ProjectBOMItem.project_id == project_id)
        .order_by(ProjectBOMItem.sort_order, ProjectBOMItem.id)
    )
    items = result.scalars().all()

    response = []
    for item in items:
        # Get archive name if linked
        archive_name = None
        if item.archive_id:
            archive_result = await db.execute(select(PrintArchive.print_name).where(PrintArchive.id == item.archive_id))
            archive_name = archive_result.scalar()

        response.append(
            BOMItemResponse(
                id=item.id,
                project_id=item.project_id,
                name=item.name,
                quantity_needed=item.quantity_needed,
                quantity_acquired=item.quantity_acquired,
                unit_price=item.unit_price,
                sourcing_url=item.sourcing_url,
                archive_id=item.archive_id,
                archive_name=archive_name,
                stl_filename=item.stl_filename,
                remarks=item.remarks,
                sort_order=item.sort_order,
                is_complete=item.quantity_acquired >= item.quantity_needed,
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
        )

    return response


@router.post("/{project_id}/bom", response_model=BOMItemResponse)
async def create_bom_item(
    project_id: int,
    data: BOMItemCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Add a BOM item to a project."""
    # Verify project exists
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    # Get max sort order
    max_order_result = await db.execute(
        select(func.max(ProjectBOMItem.sort_order)).where(ProjectBOMItem.project_id == project_id)
    )
    max_order = max_order_result.scalar() or 0

    item = ProjectBOMItem(
        project_id=project_id,
        name=data.name,
        quantity_needed=data.quantity_needed,
        unit_price=data.unit_price,
        sourcing_url=data.sourcing_url,
        archive_id=data.archive_id,
        stl_filename=data.stl_filename,
        remarks=data.remarks,
        sort_order=max_order + 1,
    )
    db.add(item)
    await db.flush()
    await db.refresh(item)

    # Get archive name if linked
    archive_name = None
    if item.archive_id:
        archive_result = await db.execute(select(PrintArchive.print_name).where(PrintArchive.id == item.archive_id))
        archive_name = archive_result.scalar()

    return BOMItemResponse(
        id=item.id,
        project_id=item.project_id,
        name=item.name,
        quantity_needed=item.quantity_needed,
        quantity_acquired=item.quantity_acquired,
        unit_price=item.unit_price,
        sourcing_url=item.sourcing_url,
        archive_id=item.archive_id,
        archive_name=archive_name,
        stl_filename=item.stl_filename,
        remarks=item.remarks,
        sort_order=item.sort_order,
        is_complete=item.quantity_acquired >= item.quantity_needed,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.patch("/{project_id}/bom/{item_id}", response_model=BOMItemResponse)
async def update_bom_item(
    project_id: int,
    item_id: int,
    data: BOMItemUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Update a BOM item."""
    result = await db.execute(
        select(ProjectBOMItem).where(
            ProjectBOMItem.id == item_id,
            ProjectBOMItem.project_id == project_id,
        )
    )
    item = result.scalar_one_or_none()

    if not item:
        raise HTTPException(status_code=404, detail="BOM item not found")

    if data.name is not None:
        item.name = data.name
    if data.quantity_needed is not None:
        item.quantity_needed = data.quantity_needed
    if data.quantity_acquired is not None:
        item.quantity_acquired = data.quantity_acquired
    if data.unit_price is not None:
        item.unit_price = data.unit_price if data.unit_price != 0 else None
    if data.sourcing_url is not None:
        item.sourcing_url = data.sourcing_url if data.sourcing_url else None
    if data.archive_id is not None:
        item.archive_id = data.archive_id if data.archive_id != 0 else None
    if data.stl_filename is not None:
        item.stl_filename = data.stl_filename if data.stl_filename else None
    if data.remarks is not None:
        item.remarks = data.remarks if data.remarks else None

    await db.flush()
    await db.refresh(item)

    # Get archive name if linked
    archive_name = None
    if item.archive_id:
        archive_result = await db.execute(select(PrintArchive.print_name).where(PrintArchive.id == item.archive_id))
        archive_name = archive_result.scalar()

    return BOMItemResponse(
        id=item.id,
        project_id=item.project_id,
        name=item.name,
        quantity_needed=item.quantity_needed,
        quantity_acquired=item.quantity_acquired,
        unit_price=item.unit_price,
        sourcing_url=item.sourcing_url,
        archive_id=item.archive_id,
        archive_name=archive_name,
        stl_filename=item.stl_filename,
        remarks=item.remarks,
        sort_order=item.sort_order,
        is_complete=item.quantity_acquired >= item.quantity_needed,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.delete("/{project_id}/bom/{item_id}")
async def delete_bom_item(
    project_id: int,
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Delete a BOM item."""
    result = await db.execute(
        select(ProjectBOMItem).where(
            ProjectBOMItem.id == item_id,
            ProjectBOMItem.project_id == project_id,
        )
    )
    item = result.scalar_one_or_none()

    if not item:
        raise HTTPException(status_code=404, detail="BOM item not found")

    await db.delete(item)

    return {"status": "success", "message": "BOM item deleted"}


@router.post("/{project_id}/create-template", response_model=ProjectResponse)
async def create_template_from_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_CREATE),
):
    """Create a template from an existing project."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    source = result.scalar_one_or_none()

    if not source:
        raise HTTPException(status_code=404, detail="Project not found")

    # Create template
    template = Project(
        name=f"{source.name} (Template)",
        description=source.description,
        color=source.color,
        target_count=source.target_count,
        target_parts_count=source.target_parts_count,
        notes=source.notes,
        tags=source.tags,
        priority=source.priority,
        budget=source.budget,
        is_template=True,
        template_source_id=source.id,
    )
    db.add(template)
    await db.flush()

    # Copy BOM items
    bom_result = await db.execute(select(ProjectBOMItem).where(ProjectBOMItem.project_id == project_id))
    bom_items = bom_result.scalars().all()

    for item in bom_items:
        new_item = ProjectBOMItem(
            project_id=template.id,
            name=item.name,
            quantity_needed=item.quantity_needed,
            quantity_acquired=0,
            unit_price=item.unit_price,
            sourcing_url=item.sourcing_url,
            stl_filename=item.stl_filename,
            remarks=item.remarks,
            sort_order=item.sort_order,
        )
        db.add(new_item)

    await db.flush()
    await db.refresh(template)

    stats = await compute_project_stats(db, template.id, template.target_count, template.target_parts_count)

    return ProjectResponse(
        id=template.id,
        name=template.name,
        description=template.description,
        color=template.color,
        status=template.status,
        target_count=template.target_count,
        target_parts_count=template.target_parts_count,
        notes=template.notes,
        attachments=template.attachments,
        tags=template.tags,
        due_date=template.due_date,
        priority=template.priority,
        budget=template.budget,
        is_template=template.is_template,
        template_source_id=template.template_source_id,
        parent_id=template.parent_id,
        parent_name=None,
        children=[],
        created_at=template.created_at,
        updated_at=template.updated_at,
        stats=stats,
    )


# ============ Duplicate an existing project ============
#
# The split users care about: **setup is copied, history is not.** Copied —
# every descriptive column, the BOM, the attached library files and folders,
# the print plan (per-file copies + order) and the uploaded attachments on
# disk. Not copied — archives and queue items, i.e. everything that records
# what this project has actually done, plus BOM ``quantity_acquired``, which
# is procurement progress rather than a part list.
#
# ⚠️ It is a COPY, never a move: the source keeps every link it had. The
# library pivots are many-to-many precisely so a file can sit in both.


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


async def _duplicate_project_tree(
    db: AsyncSession,
    source: Project,
    *,
    name: str,
    parent_id: int | None,
    include_children: bool,
    seen: set[int],
) -> Project:
    """Copy one project — and, when asked, everything under it."""
    seen.add(source.id)

    copy = Project(
        name=name,
        description=source.description,
        color=source.color,
        # ⚠️ Never inherited. A duplicate of a completed or archived project is
        # new work about to start, which is the whole reason to duplicate one.
        status="active",
        target_count=source.target_count,
        target_parts_count=source.target_parts_count,
        notes=source.notes,
        attachments=copy_module.deepcopy(source.attachments),
        tags=source.tags,
        due_date=source.due_date,
        priority=source.priority,
        budget=source.budget,
        # Duplicating a template yields another template — the flag describes
        # what the project IS, not what has happened to it.
        is_template=source.is_template,
        template_source_id=source.template_source_id,
        parent_id=parent_id,
        url=source.url,
        cover_image_filename=source.cover_image_filename,
    )
    db.add(copy)
    await db.flush()

    if (source.attachments or source.cover_image_filename) and not await _copy_attachment_files(source.id, copy.id):
        # Better an honest empty gallery than rows pointing at files that are
        # not there. The names would render as broken images with no clue why.
        copy.attachments = None
        copy.cover_image_filename = None

    bom_items = (await db.execute(select(ProjectBOMItem).where(ProjectBOMItem.project_id == source.id))).scalars().all()
    for item in bom_items:
        db.add(
            ProjectBOMItem(
                project_id=copy.id,
                name=item.name,
                quantity_needed=item.quantity_needed,
                quantity_acquired=0,  # progress, not part list
                unit_price=item.unit_price,
                sourcing_url=item.sourcing_url,
                stl_filename=item.stl_filename,
                remarks=item.remarks,
                sort_order=item.sort_order,
            )
        )

    plan_items = (
        (await db.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.project_id == source.id)))
        .scalars()
        .all()
    )
    for item in plan_items:
        db.add(
            ProjectPrintPlanItem(
                project_id=copy.id,
                library_file_id=item.library_file_id,
                copies=item.copies,
                order_index=item.order_index,
            )
        )

    # Library links. Written as pivot inserts rather than through the M2M
    # relationship so the source's collection is never loaded and therefore
    # never at risk of being reassigned instead of read.
    file_ids = (
        (
            await db.execute(
                select(library_file_projects.c.file_id).where(library_file_projects.c.project_id == source.id)
            )
        )
        .scalars()
        .all()
    )
    if file_ids:
        await db.execute(
            library_file_projects.insert(),
            [{"file_id": fid, "project_id": copy.id} for fid in file_ids],
        )
    folder_ids = (
        (
            await db.execute(
                select(library_folder_projects.c.folder_id).where(library_folder_projects.c.project_id == source.id)
            )
        )
        .scalars()
        .all()
    )
    if folder_ids:
        await db.execute(
            library_folder_projects.insert(),
            [{"folder_id": fid, "project_id": copy.id} for fid in folder_ids],
        )

    if include_children:
        children = (
            (await db.execute(select(Project).where(Project.parent_id == source.id).order_by(Project.id)))
            .scalars()
            .all()
        )
        for child in children:
            # ``seen`` guards a parent_id cycle. Nothing should be able to
            # create one, but a loop here would recurse until the process dies
            # rather than return an error.
            if child.id in seen:
                logger.warning("Project duplicate: skipping %s, already visited (parent cycle)", child.id)
                continue
            # Children keep their own names: they are already distinguished by
            # sitting under the copied parent, and "Frame (Copy)" inside
            # "Voron (Copy)" is noise.
            await _duplicate_project_tree(
                db,
                child,
                name=child.name,
                parent_id=copy.id,
                include_children=True,
                seen=seen,
            )

    return copy


@router.post("/{project_id}/duplicate", response_model=ProjectResponse)
async def duplicate_project(
    project_id: int,
    data: ProjectDuplicate = ProjectDuplicate(),  # noqa: B008 — Pydantic body default, not a Depends()
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_CREATE),
):
    """Copy a project's setup into a new active project, without its history."""
    source = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Project not found")

    taken = set((await db.execute(select(Project.name))).scalars().all())
    name = (data.name or "").strip() or _duplicate_name(source.name, taken)

    copy = await _duplicate_project_tree(
        db,
        source,
        name=name,
        parent_id=source.parent_id,  # the copy is a sibling of its source
        include_children=data.include_children,
        seen=set(),
    )

    await db.commit()
    await db.refresh(copy)

    parent_name = None
    if copy.parent_id:
        parent_name = (await db.execute(select(Project.name).where(Project.id == copy.parent_id))).scalar_one_or_none()

    children = (
        (await db.execute(select(Project).where(Project.parent_id == copy.id).order_by(Project.name))).scalars().all()
    )
    stats = await compute_project_stats(db, copy.id, copy.target_count, copy.target_parts_count)

    return ProjectResponse(
        id=copy.id,
        name=copy.name,
        description=copy.description,
        color=copy.color,
        status=copy.status,
        target_count=copy.target_count,
        target_parts_count=copy.target_parts_count,
        notes=copy.notes,
        attachments=copy.attachments,
        url=copy.url,
        cover_image_filename=copy.cover_image_filename,
        tags=copy.tags,
        due_date=copy.due_date,
        priority=copy.priority,
        budget=copy.budget,
        is_template=copy.is_template,
        template_source_id=copy.template_source_id,
        parent_id=copy.parent_id,
        parent_name=parent_name,
        children=[ProjectChildPreview(id=c.id, name=c.name, status=c.status, color=c.color) for c in children],
        created_at=copy.created_at,
        updated_at=copy.updated_at,
        stats=stats,
    )


# ============ Phase 9: Timeline Endpoint ============


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


# ============ Phase 10: Import/Export Endpoints ============


@router.get("/{project_id}/export")
async def export_project(
    project_id: int,
    format: str = "zip",  # "zip" (with files) or "json" (metadata only)
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """Export a project. Use format=zip (default) for full export with files, or format=json for metadata only."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Get BOM items
    bom_result = await db.execute(
        select(ProjectBOMItem).where(ProjectBOMItem.project_id == project_id).order_by(ProjectBOMItem.sort_order)
    )
    bom_items = bom_result.scalars().all()

    bom_export = [
        {
            "name": item.name,
            "quantity_needed": item.quantity_needed,
            "quantity_acquired": item.quantity_acquired,
            "unit_price": item.unit_price,
            "sourcing_url": item.sourcing_url,
            "stl_filename": item.stl_filename,
            "remarks": item.remarks,
        }
        for item in bom_items
    ]

    # m044: linked folders are now in the M2M pivot.
    from backend.app.models.library_project_links import library_folder_projects

    folders_result = await db.execute(
        select(LibraryFolder)
        .join(library_folder_projects, library_folder_projects.c.folder_id == LibraryFolder.id)
        .where(library_folder_projects.c.project_id == project_id)
        .order_by(LibraryFolder.name)
    )
    linked_folders = folders_result.scalars().unique().all()

    folders_export = []
    files_to_include = []  # (archive_path, zip_path)

    for folder in linked_folders:
        # Get files in this folder
        files_result = await db.execute(
            select(LibraryFile).where(LibraryFile.folder_id == folder.id).order_by(LibraryFile.filename)
        )
        files = files_result.scalars().all()

        folder_files = []
        for f in files:
            folder_files.append(
                {
                    "filename": f.filename,
                    "file_type": f.file_type,
                    "notes": f.notes,
                }
            )
            # Add file to include in ZIP
            library_dir = get_library_dir()
            file_path = library_dir / f.file_path
            if file_path.exists():
                zip_path = f"files/{folder.name}/{f.filename}"
                files_to_include.append((file_path, zip_path))
                # Also include thumbnail if exists
                if f.thumbnail_path:
                    thumb_path = library_dir / f.thumbnail_path
                    if thumb_path.exists():
                        thumb_zip_path = f"files/{folder.name}/.thumbnails/{f.filename}.png"
                        files_to_include.append((thumb_path, thumb_zip_path))

        folders_export.append(
            {
                "name": folder.name,
                "files": folder_files,
            }
        )

    # Build project JSON
    project_data = {
        "name": project.name,
        "description": project.description,
        "color": project.color,
        "status": project.status,
        "target_count": project.target_count,
        "target_parts_count": project.target_parts_count,
        "notes": project.notes,
        "tags": project.tags,
        "due_date": project.due_date.isoformat() if project.due_date else None,
        "priority": project.priority,
        "budget": project.budget,
        "bom_items": bom_export,
        "linked_folders": folders_export,
    }

    # Return JSON if requested (for bulk export)
    if format == "json":
        return project_data

    # Create ZIP in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add project.json
        zf.writestr("project.json", json.dumps(project_data, indent=2))

        # Add files
        for file_path, zip_path in files_to_include:
            zf.write(file_path, zip_path)

    zip_buffer.seek(0)

    # Generate filename
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in project.name)
    filename = f"{safe_name}_{datetime.now().strftime('%Y-%m-%d')}.zip"

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": build_content_disposition(filename)},
    )


@router.post("/import", response_model=ProjectResponse)
async def import_project(
    data: ProjectImport,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_CREATE),
):
    """Import a project with optional BOM items and linked folders."""
    # Create the project
    project = Project(
        name=data.name,
        description=data.description,
        color=data.color,
        status=data.status,
        target_count=data.target_count,
        target_parts_count=data.target_parts_count,
        notes=data.notes,
        tags=data.tags,
        due_date=data.due_date,
        priority=data.priority,
        budget=data.budget,
    )
    db.add(project)
    await db.flush()

    # Create BOM items
    for idx, bom_data in enumerate(data.bom_items):
        bom_item = ProjectBOMItem(
            project_id=project.id,
            name=bom_data.name,
            quantity_needed=bom_data.quantity_needed,
            quantity_acquired=bom_data.quantity_acquired,
            unit_price=bom_data.unit_price,
            sourcing_url=bom_data.sourcing_url,
            stl_filename=bom_data.stl_filename,
            remarks=bom_data.remarks,
            sort_order=idx,
        )
        db.add(bom_item)

    # Create linked folders in library
    for folder_data in data.linked_folders:
        # Check if folder with this name already exists at root level
        existing_result = await db.execute(
            select(LibraryFolder).where(
                LibraryFolder.name == folder_data.name,
                LibraryFolder.parent_id.is_(None),
            )
        )
        existing_folder = existing_result.scalar_one_or_none()

        if existing_folder:
            # m044: append to the existing folder's project list (don't
            # replace — the folder may already be linked to other projects).
            await db.refresh(existing_folder, attribute_names=["projects"])
            if project not in existing_folder.projects:
                existing_folder.projects.append(project)
        else:
            # Create new folder linked to this project (single-project at
            # creation; user can add more via the editor).
            new_folder = LibraryFolder(
                name=folder_data.name,
                is_external=False,
                external_readonly=False,
                external_show_hidden=False,
            )
            new_folder.projects = [project]
            db.add(new_folder)

    await db.flush()
    await db.refresh(project)

    stats = await compute_project_stats(db, project.id, project.target_count, project.target_parts_count)

    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        color=project.color,
        status=project.status,
        target_count=project.target_count,
        target_parts_count=project.target_parts_count,
        notes=project.notes,
        attachments=project.attachments,
        url=project.url,
        cover_image_filename=project.cover_image_filename,
        tags=project.tags,
        due_date=project.due_date,
        priority=project.priority,
        budget=project.budget,
        is_template=project.is_template,
        template_source_id=project.template_source_id,
        parent_id=project.parent_id,
        parent_name=None,
        children=[],
        created_at=project.created_at,
        updated_at=project.updated_at,
        stats=stats,
    )


@router.post("/import/file", response_model=ProjectResponse)
async def import_project_file(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_CREATE),
):
    """Import a project from a ZIP or JSON file."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Determine file type
    filename_lower = file.filename.lower()
    content = await file.read()

    if filename_lower.endswith(".zip"):
        # Extract project.json from ZIP
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                if "project.json" not in zf.namelist():
                    raise HTTPException(status_code=400, detail="ZIP must contain project.json")
                project_json = zf.read("project.json")
                data = json.loads(project_json)

                # Get list of files in the ZIP
                zip_files = {name: zf.read(name) for name in zf.namelist() if name.startswith("files/")}
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Invalid ZIP file")
    elif filename_lower.endswith(".json"):
        try:
            data = json.loads(content)
            zip_files = {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON file")
    else:
        raise HTTPException(status_code=400, detail="File must be .zip or .json")

    # Create the project
    project = Project(
        name=data.get("name", "Imported Project"),
        description=data.get("description"),
        color=data.get("color"),
        status=data.get("status", "active"),
        target_count=data.get("target_count"),
        target_parts_count=data.get("target_parts_count"),
        notes=data.get("notes"),
        tags=data.get("tags"),
        due_date=datetime.fromisoformat(data["due_date"]) if data.get("due_date") else None,
        priority=data.get("priority", 0),
        budget=data.get("budget"),
    )
    db.add(project)
    await db.flush()

    # Create BOM items
    for idx, bom_data in enumerate(data.get("bom_items", [])):
        bom_item = ProjectBOMItem(
            project_id=project.id,
            name=bom_data.get("name", "Unnamed"),
            quantity_needed=bom_data.get("quantity_needed", 1),
            quantity_acquired=bom_data.get("quantity_acquired", 0),
            unit_price=bom_data.get("unit_price"),
            sourcing_url=bom_data.get("sourcing_url"),
            stl_filename=bom_data.get("stl_filename"),
            remarks=bom_data.get("remarks"),
            sort_order=idx,
        )
        db.add(bom_item)

    # Create linked folders and files
    library_dir = get_library_dir()
    # Collected so their system tags can be synced after the single flush below —
    # the associations key off each row's id, which does not exist until then.
    imported_library_files: list[LibraryFile] = []
    for folder_data in data.get("linked_folders", []):
        folder_name = folder_data.get("name")
        if not folder_name:
            continue

        # Check if folder exists
        existing_result = await db.execute(
            select(LibraryFolder).where(
                LibraryFolder.name == folder_name,
                LibraryFolder.parent_id.is_(None),
            )
        )
        existing_folder = existing_result.scalar_one_or_none()

        if existing_folder:
            # m044: append to the existing folder's project list.
            await db.refresh(existing_folder, attribute_names=["projects"])
            if project not in existing_folder.projects:
                existing_folder.projects.append(project)
            folder = existing_folder
        else:
            # Create new folder linked to this project.
            folder = LibraryFolder(
                name=folder_name,
                is_external=False,
                external_readonly=False,
                external_show_hidden=False,
            )
            folder.projects = [project]
            db.add(folder)
            await db.flush()

            # Create folder on disk. ``folder_name`` comes from the uploaded
            # project.json (attacker-controlled): an absolute path collapses the
            # join and a ``..`` segment escapes ``library_dir`` — safe_join_under
            # rejects both with 400 (path-traversal hardening, GHSA-r2qv).
            folder_path = safe_join_under(library_dir, folder_name)
            folder_path.mkdir(parents=True, exist_ok=True)

        # Import files for this folder from ZIP
        folder_prefix = f"files/{folder_name}/"
        for zip_path, file_content in zip_files.items():
            if not zip_path.startswith(folder_prefix):
                continue
            if "/.thumbnails/" in zip_path:
                continue  # Skip thumbnails, we'll regenerate them

            relative_path = zip_path[len(folder_prefix) :]
            if not relative_path:
                continue

            # Write file to disk. Both ``folder_name`` and the ZIP-derived
            # ``relative_path`` are attacker-controlled (ZIP namelist() entries
            # carry ``..`` by spec); safe_join_under validates every component
            # and asserts containment before the write, closing the
            # arbitrary-file-write vector regardless of the folder branch above.
            file_disk_path = safe_join_under(library_dir, folder_name, relative_path)
            file_disk_path.parent.mkdir(parents=True, exist_ok=True)
            file_disk_path.write_bytes(file_content)

            # Determine file type via the shared helper so project-imported
            # assets render with the same badge / filter semantics as files
            # uploaded directly. Falls back to "image"/"other" buckets that
            # this code originally used for non-printable assets so existing
            # rows keep behaving the same.
            file_type = detect_file_type(relative_path)
            if file_type == "unknown":
                ext = Path(relative_path).suffix.lower()
                if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                    file_type = "image"
                else:
                    file_type = "other"

            # ⚠️ This site stored no hash at all, so project-imported files were
            # invisible to deduplication in both directions. Computing it here
            # is what puts them in.
            content_hash = hashlib.sha256(file_content).hexdigest()
            reusable = await find_reusable_row(db, content_hash=content_hash)
            if reusable is not None and reusable[1]:
                # The library already holds these bytes. Remove the copy just
                # written and link the project to the row that exists.
                file_disk_path.unlink(missing_ok=True)
                imported_library_files.append(reusable[0])
                continue

            # Create library file record
            lib_file = LibraryFile(
                folder_id=folder.id,
                filename=relative_path,
                file_path=f"{folder_name}/{relative_path}",
                file_type=file_type,
                file_size=len(file_content),
                file_hash=content_hash,
                is_external=False,
            )
            db.add(lib_file)
            imported_library_files.append(lib_file)

    await db.flush()
    # ⚠️ AFTER the flush, never in the constructor: the system-tag associations
    # key off ``lib_file.id``. This site kept the pre-m128 form — ``file_tags=``
    # in the constructor — which writes the cache column and no associations, so
    # every file imported with a project rendered its badges correctly and was
    # absent from every server-side tag filter. ``sync_system_tags`` is the one
    # writer of both representations.
    for lib_file in imported_library_files:
        await sync_system_tags(db, lib_file)
    await db.refresh(project)

    stats = await compute_project_stats(db, project.id, project.target_count, project.target_parts_count)

    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        color=project.color,
        status=project.status,
        target_count=project.target_count,
        target_parts_count=project.target_parts_count,
        notes=project.notes,
        attachments=project.attachments,
        url=project.url,
        cover_image_filename=project.cover_image_filename,
        tags=project.tags,
        due_date=project.due_date,
        priority=project.priority,
        budget=project.budget,
        is_template=project.is_template,
        template_source_id=project.template_source_id,
        parent_id=project.parent_id,
        parent_name=None,
        children=[],
        created_at=project.created_at,
        updated_at=project.updated_at,
        stats=stats,
    )


# ============ Print Plan ============


def _build_plan_item_response(
    row: ProjectPrintPlanItem,
    file: LibraryFile,
    default_cost_per_kg: float,
    printed_count: int,
) -> PrintPlanItemResponse:
    """Derive per-row totals from the joined library file's metadata."""
    meta = file.file_metadata or {}
    grams = meta.get("filament_used_grams")
    grams = float(grams) if isinstance(grams, (int, float)) else None
    secs = meta.get("print_time_seconds")
    secs = int(secs) if isinstance(secs, (int, float)) else None
    objs = meta.get("printable_objects")
    obj_count = len(objs) if isinstance(objs, dict) else None
    # None, not 0.00: with no farm rate set there is nothing to say about
    # what a copy costs, and a zero reads as "free".
    cost_per_copy = (
        round(grams / 1000 * default_cost_per_kg, 2) if grams is not None and default_cost_per_kg > 0 else None
    )

    total_grams = round(grams * row.copies, 2) if grams is not None else None
    total_secs = secs * row.copies if secs is not None else None
    total_objs = obj_count * row.copies if obj_count is not None else None
    total_cost = round(cost_per_copy * row.copies, 2) if cost_per_copy is not None else None

    return PrintPlanItemResponse(
        id=row.id,
        library_file_id=row.library_file_id,
        copies=row.copies,
        order_index=row.order_index,
        filename=file.filename,
        print_name=(meta.get("print_name") if isinstance(meta.get("print_name"), str) else None),
        file_type=file.file_type,
        thumbnail_path=file.thumbnail_path,
        swap_compatible=file.swap_compatible,
        filament_grams=grams,
        print_time_seconds=secs,
        object_count=obj_count,
        cost_per_copy=cost_per_copy,
        total_filament_grams=total_grams,
        total_print_time_seconds=total_secs,
        total_objects=total_objs,
        total_cost=total_cost,
        # Clamp remainder at 0 — an operator who lowers ``copies`` below
        # the already-printed count shouldn't see a negative "remaining".
        printed_count=printed_count,
        remaining_count=max(0, row.copies - printed_count),
    )


async def _get_default_filament_cost(db: AsyncSession) -> float:
    """The farm's rate, or 0.0 when unset — the same answer everything else
    gets. See ``services/filament_cost``."""
    from backend.app.services.filament_cost import default_rate_per_kg

    return await default_rate_per_kg(db)


async def _load_print_plan(db: AsyncSession, project_id: int) -> PrintPlanResponse:
    rows = (
        await db.execute(
            select(ProjectPrintPlanItem, LibraryFile)
            .join(LibraryFile, ProjectPrintPlanItem.library_file_id == LibraryFile.id)
            .where(ProjectPrintPlanItem.project_id == project_id)
            .order_by(ProjectPrintPlanItem.order_index, ProjectPrintPlanItem.id)
        )
    ).all()

    default_cost_per_kg = await _get_default_filament_cost(db)

    # Per-(project, library_file) printed-count: completed archives only.
    # One bulk query rather than per-row to keep the endpoint flat.
    file_ids = [row.library_file_id for row, _ in rows]
    printed_counts: dict[int, int] = {}
    if file_ids:
        printed_rows = (
            await db.execute(
                select(PrintArchive.library_file_id, func.count(PrintArchive.id))
                .where(
                    PrintArchive.project_id == project_id,
                    PrintArchive.library_file_id.in_(file_ids),
                    PrintArchive.status == "completed",
                )
                .group_by(PrintArchive.library_file_id)
            )
        ).all()
        printed_counts = dict(printed_rows)

    items = [
        _build_plan_item_response(row, file, default_cost_per_kg, printed_counts.get(row.library_file_id, 0))
        for row, file in rows
    ]

    return PrintPlanResponse(
        items=items,
        totals_filament_grams=round(sum((i.total_filament_grams or 0) for i in items), 2),
        totals_print_time_seconds=int(sum((i.total_print_time_seconds or 0) for i in items)),
        totals_objects=int(sum((i.total_objects or 0) for i in items)),
        totals_cost=round(sum((i.total_cost or 0) for i in items), 2),
        default_filament_cost_per_kg=default_cost_per_kg,
    )


@router.get("/{project_id}/print-plan", response_model=PrintPlanResponse)
async def get_project_print_plan(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """Return the ordered print plan for a project with computed totals."""
    project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return await _load_print_plan(db, project_id)


@router.patch("/{project_id}/print-plan/{library_file_id}", response_model=PrintPlanItemResponse)
async def update_project_print_plan_item(
    project_id: int,
    library_file_id: int,
    body: PrintPlanItemUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Update a plan row's ``copies``. Minimum 1 — 0 means unlink via file update."""
    if body.copies < 1:
        raise HTTPException(status_code=400, detail="copies must be >= 1")

    row = (
        await db.execute(
            select(ProjectPrintPlanItem).where(
                ProjectPrintPlanItem.project_id == project_id,
                ProjectPrintPlanItem.library_file_id == library_file_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Print plan row not found")

    file = (await db.execute(select(LibraryFile).where(LibraryFile.id == library_file_id))).scalar_one_or_none()
    if file is None:
        raise HTTPException(status_code=404, detail="Library file not found")

    row.copies = body.copies
    await db.commit()
    await db.refresh(row)

    default_cost_per_kg = await _get_default_filament_cost(db)
    printed_count = (
        await db.execute(
            select(func.count(PrintArchive.id)).where(
                PrintArchive.project_id == project_id,
                PrintArchive.library_file_id == library_file_id,
                PrintArchive.status == "completed",
            )
        )
    ).scalar() or 0
    return _build_plan_item_response(row, file, default_cost_per_kg, printed_count)


@router.post("/{project_id}/print-plan/reorder", response_model=PrintPlanResponse)
async def reorder_project_print_plan(
    project_id: int,
    body: PrintPlanReorderRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Assign ``order_index`` = position-in-list for the provided file IDs.

    Rows not mentioned keep their existing order_index. This lets the client
    ship a stable ID list without knowing about rows that appeared between
    reads (e.g. a concurrent folder link adding a new file).
    """
    rows_by_file = {
        r.library_file_id: r
        for r in (
            await db.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.project_id == project_id))
        ).scalars()
    }

    missing = [fid for fid in body.library_file_ids if fid not in rows_by_file]
    if missing:
        raise HTTPException(status_code=404, detail=f"Plan rows not found for files: {missing}")

    for pos, fid in enumerate(body.library_file_ids):
        rows_by_file[fid].order_index = pos

    await db.commit()
    return await _load_print_plan(db, project_id)


# ============ Parts Ledger Endpoints (m158) ============


async def _read_project_parts(db: AsyncSession, project_id: int) -> ProjectPartsResponse:
    """The parts ledger: targets merged with per-part print history.

    History rows without a target are returned too (target_qty=None) —
    what was printed is never hidden by not having set a goal for it.
    """
    targets = (await db.execute(select(ProjectPart).where(ProjectPart.project_id == project_id))).scalars().all()

    agg_rows = (
        await db.execute(
            select(
                PrintArchivePart.name_key,
                func.max(PrintArchivePart.name).label("name"),
                func.coalesce(
                    func.sum(case((PrintArchive.status == "completed", PrintArchivePart.quantity), else_=0)), 0
                ).label("printed"),
                func.coalesce(
                    func.sum(case((PrintArchive.status == "completed", PrintArchivePart.defective), else_=0)), 0
                ).label("defective"),
                func.coalesce(
                    func.sum(case((PrintArchive.status == "printing", PrintArchivePart.quantity), else_=0)), 0
                ).label("in_progress"),
            )
            .join(PrintArchive, PrintArchive.id == PrintArchivePart.archive_id)
            .where(PrintArchive.project_id == project_id, PrintArchive.deleted_at.is_(None))
            .group_by(PrintArchivePart.name_key)
        )
    ).all()
    history = {row.name_key: row for row in agg_rows}

    parts: list[ProjectPartRow] = []
    for target in targets:
        h = history.pop(target.name_key, None)
        printed = int(h.printed) if h else 0
        defective = int(h.defective) if h else 0
        usable = max(0, printed - defective)
        parts.append(
            ProjectPartRow(
                name=target.name,
                name_key=target.name_key,
                target_qty=target.target_qty,
                printed=printed,
                in_progress=int(h.in_progress) if h else 0,
                defective=defective,
                usable=usable,
                remaining=max(0, target.target_qty - usable),
            )
        )
    for key, h in history.items():  # history without a target
        printed = int(h.printed)
        defective = int(h.defective)
        parts.append(
            ProjectPartRow(
                name=h.name,
                name_key=key,
                target_qty=None,
                printed=printed,
                in_progress=int(h.in_progress),
                defective=defective,
                usable=max(0, printed - defective),
                remaining=None,
            )
        )
    parts.sort(key=lambda p: p.name_key)
    return ProjectPartsResponse(parts=parts)


@router.get("/{project_id}/parts", response_model=ProjectPartsResponse)
async def get_project_parts(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """The parts ledger: targets merged with per-part print history."""
    return await _read_project_parts(db, project_id)


@router.patch("/{project_id}/parts", response_model=ProjectPartsResponse)
async def update_project_parts(
    project_id: int,
    data: ProjectPartsUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Upsert target quantities by name_key."""
    existing = {
        r.name_key: r
        for r in (await db.execute(select(ProjectPart).where(ProjectPart.project_id == project_id))).scalars().all()
    }
    for item in data.parts:
        row = existing.get(item.name_key)
        if row is not None:
            row.target_qty = item.target_qty
        else:
            db.add(
                ProjectPart(
                    project_id=project_id,
                    name=item.name or item.name_key,
                    name_key=item.name_key,
                    target_qty=item.target_qty,
                )
            )
    await db.commit()
    return await _read_project_parts(db, project_id)


@router.delete("/{project_id}/parts")
async def delete_project_part(
    project_id: int,
    name_key: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    """Remove a target row (query param dodges name_key URL-encoding traps).

    History rows are untouched — the part simply goes back to 'untargeted'.
    """
    await db.execute(delete(ProjectPart).where(ProjectPart.project_id == project_id, ProjectPart.name_key == name_key))
    await db.commit()
    return {"status": "ok"}
