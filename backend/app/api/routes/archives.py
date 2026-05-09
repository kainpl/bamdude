import io
import json
import logging
import zipfile
from collections import defaultdict
from datetime import date, datetime, time, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import (
    RequirePermission,
    require_ownership_permission,
)
from backend.app.core.config import settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.models.user import User
from backend.app.schemas.archive import (
    ArchiveFilterOptions,
    ArchiveResponse,
    ArchiveSlim,
    ArchiveStats,
    ArchiveUpdate,
    PaginatedArchiveResponse,
    PaginationMeta,
    ReprintRequest,
)
from backend.app.services.archive import ArchiveService, resolve_display_stem
from backend.app.services.threemf_capabilities import extract_3mf_capabilities
from backend.app.utils.threemf_tools import (
    extract_nozzle_mapping_from_3mf,
    extract_project_filaments_from_3mf,
    extract_source_printer_model_from_3mf,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/archives", tags=["archives"])


def _safe_filename(name: str) -> str:
    """Sanitize upload filename to prevent path traversal."""
    return Path(name.replace("\\", "/")).name


def _validate_user_filter_permission(current_user: User | None, created_by_id: int | None):
    """Raise 403 if created_by_id filter is used without stats:filter_by_user permission."""
    if created_by_id is None or current_user is None:
        return
    if current_user.is_admin:
        return
    if not current_user.has_permission(Permission.STATS_FILTER_BY_USER.value):
        raise HTTPException(status_code=403, detail="Permission stats:filter_by_user required")


def _apply_user_filter(conditions: list, created_by_id: int | None):
    """Append created_by_id filter to conditions list if specified."""
    if created_by_id is not None:
        if created_by_id == -1:
            conditions.append(PrintArchive.created_by_id.is_(None))
        else:
            conditions.append(PrintArchive.created_by_id == created_by_id)


def compute_time_accuracy(archive: PrintArchive) -> dict:
    """Compute actual print time and accuracy for an archive.

    Returns dict with actual_time_seconds and time_accuracy.
    time_accuracy = (estimated / actual) * 100
    - 100% = perfect estimate
    - >100% = print was faster than estimated
    - <100% = print took longer than estimated
    """
    result = {"actual_time_seconds": None, "time_accuracy": None}

    if archive.started_at and archive.completed_at and archive.status == "completed":
        actual_seconds = int((archive.completed_at - archive.started_at).total_seconds())
        if actual_seconds > 0:
            result["actual_time_seconds"] = actual_seconds

            if archive.print_time_seconds and archive.print_time_seconds > 0:
                # Calculate accuracy as percentage
                accuracy = (archive.print_time_seconds / actual_seconds) * 100
                # Sanity check: skip unreasonable values (e.g., manually changed status)
                # Valid range: 5% to 500% (print took 20x longer to 5x faster than estimated)
                if 5 <= accuracy <= 500:
                    result["time_accuracy"] = round(accuracy, 1)

    return result


def _parse_applied_patches(raw: str | None) -> list[str] | None:
    """Decode the ``applied_patches`` TEXT column into a list for response."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, list) else None


def archive_to_response(
    archive: PrintArchive,
    duplicates: list[dict] | None = None,
    duplicate_count: int = 0,
    duplicate_sequence: int = 0,
    original_archive_id: int | None = None,
) -> dict:
    """Convert archive model to response dict with computed fields."""
    data = {
        "id": archive.id,
        "printer_id": archive.printer_id,
        "project_id": archive.project_id,
        "project_name": archive.project.name if archive.project else None,
        "filename": archive.filename,
        "file_path": archive.file_path,
        "file_size": archive.file_size,
        "content_hash": archive.content_hash,
        "source_content_hash": archive.source_content_hash,
        "applied_patches": _parse_applied_patches(archive.applied_patches),
        "effective_hash": archive.source_content_hash or archive.content_hash,
        "thumbnail_path": archive.thumbnail_path,
        "timelapse_path": archive.timelapse_path,
        "source_3mf_path": archive.source_3mf_path,
        "f3d_path": archive.f3d_path,
        "duplicates": duplicates,
        "duplicate_count": duplicate_count if duplicates is None else len(duplicates),
        "duplicate_sequence": duplicate_sequence,
        "original_archive_id": original_archive_id,
        "print_name": archive.print_name,
        "print_time_seconds": archive.print_time_seconds,
        "filament_used_grams": archive.filament_used_grams,
        "filament_type": archive.filament_type,
        "filament_color": archive.filament_color,
        "layer_height": archive.layer_height,
        "total_layers": archive.total_layers,
        "nozzle_diameter": archive.nozzle_diameter,
        "bed_temperature": archive.bed_temperature,
        "nozzle_temperature": archive.nozzle_temperature,
        "sliced_for_model": archive.sliced_for_model,
        "status": archive.status,
        "started_at": archive.started_at,
        "completed_at": archive.completed_at,
        "extra_data": archive.extra_data,
        "makerworld_url": archive.makerworld_url,
        "designer": archive.designer,
        "external_url": archive.external_url,
        "is_favorite": archive.is_favorite,
        "tags": archive.tags,
        "notes": archive.notes,
        "cost": archive.cost,
        "photos": archive.photos,
        "failure_reason": archive.failure_reason,
        "quantity": archive.quantity,
        "energy_kwh": archive.energy_kwh,
        "energy_cost": archive.energy_cost,
        # Queue attribution (m019) + verbose error_message twin for failures.
        "queue_id": archive.queue_id,
        "batch_id": archive.batch_id,
        "error_message": archive.error_message,
        "created_at": archive.created_at,
        # User tracking (Issue #206)
        "created_by_id": archive.created_by_id,
        "created_by_username": archive.created_by.username if archive.created_by else None,
    }

    # Add computed time accuracy fields
    accuracy_data = compute_time_accuracy(archive)
    data.update(accuracy_data)

    return data


@router.get("/", response_model=PaginatedArchiveResponse)
async def list_archives(
    printer_id: int | None = None,
    project_id: int | None = None,
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    search: str | None = Query(None),
    collection: str | None = Query(None),
    material: str | None = Query(None),
    colors: str | None = Query(None, description="Comma-separated color hex values"),
    color_mode: str = Query("or", description="'or' or 'and'"),
    favorites_only: bool = Query(False),
    hide_failed: bool = Query(False),
    hide_duplicates: bool = Query(False),
    tag: str | None = Query(None),
    file_type: str | None = Query(None, description="'gcode' or 'source'"),
    sort_by: str = Query("date-desc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(24, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """List archived prints with server-side filtering, sorting and pagination."""
    service = ArchiveService(db)

    # Parse comma-separated colors
    color_list = [c.strip() for c in colors.split(",") if c.strip()] if colors else None

    offset = (page - 1) * per_page
    archives, total = await service.list_archives(
        printer_id=printer_id,
        project_id=project_id,
        date_from=date_from,
        date_to=date_to,
        search=search,
        collection=collection,
        material=material,
        colors=color_list,
        color_mode=color_mode,
        favorites_only=favorites_only,
        hide_failed=hide_failed,
        hide_duplicates=hide_duplicates,
        tag=tag,
        file_type=file_type,
        sort_by=sort_by,
        limit=per_page,
        offset=offset,
    )

    # Get sets of duplicate hashes and duplicate (name, hash) pairs (efficient single queries).
    # The service returns *effective* hashes (COALESCE(source_content_hash, content_hash)) so
    # patched archives group with their unpatched ancestors. Page-side matching below must
    # therefore project each row to its effective hash too — `source_content_hash` first,
    # `content_hash` as defence fallback for any (post-m039) row that still has NULL source.
    duplicate_hashes, duplicate_name_hash_pairs = await service.get_duplicate_hashes_and_names()

    def _eff(archive: PrintArchive) -> str | None:
        return archive.source_content_hash or archive.content_hash

    # SQL-side equivalent for the secondary load query.
    eff_hash_col = func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash)

    # Batch-load duplicate groups once for the current page keys.
    duplicate_hashes_in_page = {h for a in archives if (h := _eff(a)) and h in duplicate_hashes}
    duplicate_name_hash_keys_in_page = {
        (a.print_name.lower(), h)
        for a in archives
        if a.print_name and (h := _eff(a)) and (a.print_name.lower(), h) in duplicate_name_hash_pairs
    }

    duplicate_meta_by_archive_id: dict[int, tuple[int, int, int]] = {}

    if duplicate_hashes_in_page or duplicate_name_hash_keys_in_page:
        duplicate_group_conditions = []
        if duplicate_hashes_in_page:
            duplicate_group_conditions.append(eff_hash_col.in_(duplicate_hashes_in_page))
        if duplicate_name_hash_keys_in_page:
            name_hash_conditions = [
                and_(func.lower(PrintArchive.print_name) == name, eff_hash_col == hash_)
                for name, hash_ in duplicate_name_hash_keys_in_page
            ]
            duplicate_group_conditions.extend(name_hash_conditions)

        duplicate_group_rows = await db.execute(
            select(
                PrintArchive.id,
                PrintArchive.created_at,
                eff_hash_col.label("effective_hash"),
                func.lower(PrintArchive.print_name).label("print_name_lower"),
            ).where(or_(*duplicate_group_conditions))
        )

        duplicate_groups_by_hash: dict[str, list[tuple[int, datetime]]] = defaultdict(list)
        duplicate_groups_by_name_hash: dict[tuple[str, str], list[tuple[int, datetime]]] = defaultdict(list)

        for archive_id, created_at, effective_hash, print_name_lower in duplicate_group_rows.all():
            if effective_hash and effective_hash in duplicate_hashes_in_page:
                duplicate_groups_by_hash[effective_hash].append((archive_id, created_at))
            if (
                print_name_lower
                and effective_hash
                and (print_name_lower, effective_hash) in duplicate_name_hash_keys_in_page
            ):
                duplicate_groups_by_name_hash[(print_name_lower, effective_hash)].append((archive_id, created_at))

        for group in duplicate_groups_by_hash.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda x: x[1])
            original_id = group[0][0]
            duplicate_count = len(group) - 1
            for sequence, (archive_id, _) in enumerate(group):
                duplicate_meta_by_archive_id[archive_id] = (sequence, original_id, duplicate_count)

        # Keep hash-based grouping precedence; name/hash groups only fill missing items.
        for group in duplicate_groups_by_name_hash.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda x: x[1])
            original_id = group[0][0]
            duplicate_count = len(group) - 1
            for sequence, (archive_id, _) in enumerate(group):
                duplicate_meta_by_archive_id.setdefault(archive_id, (sequence, original_id, duplicate_count))

    # Build response with duplicate sequence and original archive ID pre-computed
    data = []
    for a in archives:
        a_eff = _eff(a)
        has_hash_dup = a_eff in duplicate_hashes if a_eff else False
        has_name_dup = bool(a.print_name and a_eff) and (a.print_name.lower(), a_eff) in duplicate_name_hash_pairs
        has_duplicate = has_hash_dup or has_name_dup

        # Pre-compute duplicate sequence and original archive ID
        duplicate_sequence = 0
        original_archive_id: int | None = None
        duplicate_count = 1 if has_duplicate else 0

        if has_duplicate and a.id in duplicate_meta_by_archive_id:
            duplicate_sequence, original_archive_id, duplicate_count = duplicate_meta_by_archive_id[a.id]

        data.append(
            archive_to_response(
                a,
                duplicate_count=duplicate_count,
                duplicate_sequence=duplicate_sequence,
                original_archive_id=original_archive_id,
            )
        )

    import math

    last_page = max(1, math.ceil(total / per_page))

    return PaginatedArchiveResponse(
        data=data,
        meta=PaginationMeta(
            total=total,
            current_page=page,
            per_page=per_page,
            last_page=last_page,
        ),
    )


@router.get("/filter-options", response_model=ArchiveFilterOptions)
async def get_archive_filter_options(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Get distinct filter values for archive dropdowns (materials, colors, tags)."""
    service = ArchiveService(db)
    options = await service.get_filter_options()
    return ArchiveFilterOptions(**options)


@router.get("/slim", response_model=list[ArchiveSlim])
async def list_archives_slim(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    limit: int = Query(default=10000, le=50000),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Lightweight archive listing for stats/dashboard widgets.

    Returns only the fields needed for client-side aggregation,
    skipping duplicate detection, file paths, and extra_data.
    """
    # Exclude "archived" status - uploaded but never printed
    filters = [PrintArchive.status != "archived"]
    if date_from:
        dt_from = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
        filters.append(PrintArchive.created_at >= dt_from)
    if date_to:
        dt_to = datetime.combine(date_to, time.max, tzinfo=timezone.utc)
        filters.append(PrintArchive.created_at <= dt_to)

    query = (
        select(
            PrintArchive.id,
            PrintArchive.printer_id,
            PrintArchive.print_name,
            PrintArchive.filename,
            PrintArchive.print_time_seconds,
            PrintArchive.started_at,
            PrintArchive.completed_at,
            PrintArchive.filament_used_grams,
            PrintArchive.filament_type,
            PrintArchive.filament_color,
            PrintArchive.status,
            PrintArchive.cost,
            PrintArchive.quantity,
            PrintArchive.created_at,
            PrintArchive.thumbnail_path,
        )
        .where(*filters)
        .order_by(PrintArchive.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(query)
    rows = result.all()

    return [
        {
            "id": r.id,
            "printer_id": r.printer_id,
            "print_name": r.print_name,
            "filename": r.filename,
            "print_time_seconds": r.print_time_seconds,
            "actual_time_seconds": (
                int((r.completed_at - r.started_at).total_seconds())
                if r.started_at
                and r.completed_at
                and r.status == "completed"
                and (r.completed_at - r.started_at).total_seconds() > 0
                else None
            ),
            "filament_used_grams": r.filament_used_grams,
            "filament_type": r.filament_type,
            "filament_color": r.filament_color,
            "status": r.status,
            "started_at": r.started_at,
            "completed_at": r.completed_at,
            "cost": r.cost,
            "quantity": r.quantity,
            "created_at": r.created_at,
            "thumbnail_path": r.thumbnail_path,
        }
        for r in rows
    ]


@router.get("/search", response_model=list[ArchiveResponse])
async def search_archives(
    q: str = Query(..., min_length=2, description="Search query"),
    printer_id: int | None = None,
    project_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Full-text search across archives.

    Searches print_name, filename, tags, notes, designer, and filament_type fields.
    Supports partial matches with wildcards (e.g., 'vor*' matches 'voron').
    """
    from sqlalchemy import text
    from sqlalchemy.orm import selectinload

    from backend.app.core.db_dialect import is_postgres

    search_term = q.strip()

    # Build dialect-specific FTS query
    if is_postgres():
        # PostgreSQL: tsvector + ts_query with prefix matching
        pg_term = " & ".join(f"{w}:*" for w in search_term.split() if w)
        fts_query = text("""
            SELECT id FROM print_archives
            WHERE search_vector @@ to_tsquery('simple', :search_term)
            ORDER BY ts_rank(search_vector, to_tsquery('simple', :search_term)) DESC
            LIMIT :limit OFFSET :offset
        """)
        fts_params = {"search_term": pg_term, "limit": limit + 100, "offset": 0}
    else:
        # SQLite: FTS5 MATCH
        if not search_term.endswith("*"):
            search_term = f"{search_term}*"
        fts_query = text("""
            SELECT rowid FROM archive_fts
            WHERE archive_fts MATCH :search_term
            ORDER BY rank
            LIMIT :limit OFFSET :offset
        """)
        fts_params = {"search_term": search_term, "limit": limit + 100, "offset": 0}

    try:
        result = await db.execute(fts_query, fts_params)
        matched_ids = [row[0] for row in result.fetchall()]
    except Exception as e:
        logger.warning("FTS search failed, falling back to LIKE search: %s", e)
        # Fallback to LIKE search if FTS fails
        like_pattern = f"%{q}%"
        query = (
            select(PrintArchive)
            .options(selectinload(PrintArchive.project))
            .where(
                (
                    (PrintArchive.print_name.ilike(like_pattern))
                    | (PrintArchive.filename.ilike(like_pattern))
                    | (PrintArchive.tags.ilike(like_pattern))
                    | (PrintArchive.notes.ilike(like_pattern))
                    | (PrintArchive.designer.ilike(like_pattern))
                    | (PrintArchive.filament_type.ilike(like_pattern))
                ),
                PrintArchive.deleted_at.is_(None),
            )
            .order_by(PrintArchive.created_at.desc())
        )

        if printer_id:
            query = query.where(PrintArchive.printer_id == printer_id)
        if project_id:
            query = query.where(PrintArchive.project_id == project_id)
        if status:
            query = query.where(PrintArchive.status == status)

        query = query.limit(limit).offset(offset)
        result = await db.execute(query)
        archives = result.scalars().all()
        return [archive_to_response(a) for a in archives]

    if not matched_ids:
        return []

    # Fetch full archive records for matched IDs (excluding trashed).
    query = (
        select(PrintArchive)
        .options(selectinload(PrintArchive.project))
        .where(PrintArchive.id.in_(matched_ids), PrintArchive.deleted_at.is_(None))
    )

    # Apply additional filters
    if printer_id:
        query = query.where(PrintArchive.printer_id == printer_id)
    if project_id:
        query = query.where(PrintArchive.project_id == project_id)
    if status:
        query = query.where(PrintArchive.status == status)

    result = await db.execute(query)
    archives_dict = {a.id: a for a in result.scalars().all()}

    # Preserve FTS ranking order and apply pagination
    ordered_archives = [archives_dict[id] for id in matched_ids if id in archives_dict]
    paginated = ordered_archives[offset : offset + limit]

    return [archive_to_response(a) for a in paginated]


@router.get("/cleanup/status")
async def archive_cleanup_status(
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Status of the 3MF auto-cleanup loop.

    Returns: enabled flag, retention window, last run summary, next
    scheduled run time. Drives the "Archive cleanup" panel on the
    settings page so the operator can see the loop is alive without
    digging in logs.

    ``last_run.archives_cleared = -1`` is a restart-volatile sentinel —
    the persistent timestamp survived a process restart but the in-memory
    counts didn't. Frontend renders the timestamp without the count in
    that case.
    """
    from backend.app.services.archive_cleanup_service import archive_cleanup_service

    return await archive_cleanup_service.get_status()


@router.get("/cleanup/preview")
async def archive_cleanup_preview(
    days: int | None = Query(default=None, ge=1, le=3650),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Dry-run: how many archives would lose their 3MF right now?

    Reads the same skip rules as the live sweep but does not touch disk
    or DB. Without ``days`` uses the configured retention threshold; pass
    ``days=N`` for an ad-hoc preview at a different threshold (Archive
    page modal lets the operator override per-run).
    """
    from backend.app.services.archive_cleanup_service import archive_cleanup_service

    return await archive_cleanup_service.preview(override_days=days)


@router.post("/cleanup/run")
async def archive_cleanup_run(
    days: int | None = Query(default=None, ge=1, le=3650),
    _: User | None = RequirePermission(Permission.ARCHIVES_DELETE_ALL),
):
    """Trigger an immediate cleanup sweep, bypassing the daily cron.

    Returns the run summary (same shape as ``last_run`` in
    ``/cleanup/status``). Without ``days`` uses the configured retention
    threshold; ``days=N`` overrides for one ad-hoc run.
    """
    from backend.app.services.archive_cleanup_service import archive_cleanup_service

    result = await archive_cleanup_service.run_now(override_days=days)
    return result.as_dict()


@router.post("/search/rebuild-index")
async def rebuild_search_index(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_ALL),
):
    """Rebuild the full-text search index from existing archives.

    Use this if search results seem incomplete or incorrect.
    """
    from sqlalchemy import text

    from backend.app.core.db_dialect import is_postgres

    try:
        if is_postgres():
            # PostgreSQL: re-trigger tsvector update for all rows
            await db.execute(text("UPDATE print_archives SET print_name = print_name"))
            await db.commit()
            result = await db.execute(text("SELECT COUNT(*) FROM print_archives WHERE search_vector IS NOT NULL"))
        else:
            # SQLite: clear and rebuild FTS5 index
            await db.execute(text("DELETE FROM archive_fts"))
            await db.execute(
                text("""
                INSERT INTO archive_fts(rowid, print_name, filename, tags, notes, designer, filament_type)
                SELECT id, print_name, filename, tags, notes, designer, filament_type
                FROM print_archives
            """)
            )
            await db.commit()
            result = await db.execute(text("SELECT COUNT(*) FROM archive_fts"))

        count = result.scalar() or 0
        return {"message": f"Search index rebuilt with {count} entries"}
    except Exception as e:
        logger.error("Failed to rebuild search index: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to rebuild index: {str(e)}")


@router.get("/analysis/failures")
async def analyze_failures(
    days: int | None = None,
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    printer_id: int | None = None,
    project_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Analyze failure patterns across prints.

    Returns failure statistics including:
    - Overall failure rate
    - Failures by reason, filament type, printer
    - Time of day distribution
    - Recent failures
    - Weekly trend
    """
    from backend.app.services.failure_analysis import FailureAnalysisService

    service = FailureAnalysisService(db)
    return await service.analyze_failures(
        days=days,
        date_from=date_from,
        date_to=date_to,
        printer_id=printer_id,
        project_id=project_id,
    )


@router.get("/compare")
async def compare_archives(
    archive_ids: str = Query(..., description="Comma-separated archive IDs (2-5)"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Compare multiple archives side by side.

    Compares print settings, filament usage, and print times.
    Also analyzes correlation between settings and success/failure.

    Args:
        archive_ids: Comma-separated list of 2-5 archive IDs to compare
    """
    from backend.app.services.archive_comparison import ArchiveComparisonService

    # Parse and validate archive IDs
    try:
        ids = [int(id.strip()) for id in archive_ids.split(",")]
    except ValueError:
        raise HTTPException(400, "Invalid archive IDs format")

    if len(ids) < 2:
        raise HTTPException(400, "At least 2 archives required for comparison")
    if len(ids) > 5:
        raise HTTPException(400, "Maximum 5 archives can be compared at once")

    service = ArchiveComparisonService(db)
    try:
        return await service.compare_archives(ids)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/export")
async def export_archives(
    format: str = Query("csv", description="Export format: csv or xlsx"),
    fields: str | None = Query(None, description="Comma-separated field names"),
    printer_id: int | None = None,
    project_id: int | None = None,
    status: str | None = None,
    date_from: str | None = Query(None, description="Start date (ISO format)"),
    date_to: str | None = Query(None, description="End date (ISO format)"),
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Export archives to CSV or Excel format.

    Returns a downloadable file with archive data.
    """
    from datetime import datetime

    from fastapi.responses import StreamingResponse

    from backend.app.services.export import ExportService

    if format not in ("csv", "xlsx"):
        raise HTTPException(400, "Format must be 'csv' or 'xlsx'")

    # Parse fields
    field_list = None
    if fields:
        field_list = [f.strip() for f in fields.split(",")]

    # Parse dates
    date_from_dt = None
    date_to_dt = None
    if date_from:
        try:
            date_from_dt = datetime.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(400, "Invalid date_from format")
    if date_to:
        try:
            date_to_dt = datetime.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(400, "Invalid date_to format")

    service = ExportService(db)
    try:
        file_bytes, filename, content_type = await service.export_archives(
            format=format,
            fields=field_list,
            printer_id=printer_id,
            project_id=project_id,
            status=status,
            date_from=date_from_dt,
            date_to=date_to_dt,
            search=search,
        )
    except ImportError as e:
        raise HTTPException(500, str(e))

    return StreamingResponse(
        io.BytesIO(file_bytes),
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/stats/export")
async def export_stats(
    format: str = Query("csv", description="Export format: csv or xlsx"),
    days: int = 30,
    printer_id: int | None = None,
    project_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.STATS_READ),
):
    """Export statistics summary to CSV or Excel format."""
    from fastapi.responses import StreamingResponse

    from backend.app.services.export import ExportService

    if format not in ("csv", "xlsx"):
        raise HTTPException(400, "Format must be 'csv' or 'xlsx'")

    service = ExportService(db)
    try:
        file_bytes, filename, content_type = await service.export_stats(
            format=format,
            days=days,
            printer_id=printer_id,
            project_id=project_id,
        )
    except ImportError as e:
        raise HTTPException(500, str(e))

    return StreamingResponse(
        io.BytesIO(file_bytes),
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/stats", response_model=ArchiveStats)
async def get_archive_stats(
    date_from: date | None = Query(None, description="Start date (inclusive), YYYY-MM-DD"),
    date_to: date | None = Query(None, description="End date (inclusive), YYYY-MM-DD"),
    created_by_id: int | None = Query(None, description="Filter by user who created the print (-1 for no user)"),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.STATS_READ),
):
    """Get statistics across all archives."""
    _validate_user_filter_permission(current_user, created_by_id)

    # Build date filter conditions
    # Exclude "archived" status - these are files uploaded via virtual printer
    # or manual upload that were never actually printed.
    # Exclude trashed rows (deleted_at IS NOT NULL) — trash is a soft-delete
    # awaiting the retention sweeper, the user has explicitly removed these
    # from active history and they shouldn't pollute totals / filament / cost.
    base_conditions = [
        PrintArchive.status != "archived",
        PrintArchive.deleted_at.is_(None),
    ]
    _apply_user_filter(base_conditions, created_by_id)
    if date_from:
        dt_from = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
        base_conditions.append(PrintArchive.created_at >= dt_from)
    if date_to:
        dt_to = datetime.combine(date_to, time.max, tzinfo=timezone.utc)
        base_conditions.append(PrintArchive.created_at <= dt_to)

    # Total counts
    total_result = await db.execute(select(func.count(PrintArchive.id)).where(*base_conditions))
    total_prints = total_result.scalar() or 0

    successful_result = await db.execute(
        select(func.count(PrintArchive.id)).where(PrintArchive.status == "completed", *base_conditions)
    )
    successful_prints = successful_result.scalar() or 0

    failed_result = await db.execute(
        select(func.count(PrintArchive.id)).where(
            PrintArchive.status.in_(["failed", "aborted", "cancelled"]), *base_conditions
        )
    )
    failed_prints = failed_result.scalar() or 0

    # Totals - use actual print time from timestamps (not slicer estimates)
    # For archives with both started_at and completed_at, calculate actual duration
    # Fall back to print_time_seconds only for archives without timestamps
    archives_for_time = await db.execute(
        select(PrintArchive.started_at, PrintArchive.completed_at, PrintArchive.print_time_seconds).where(
            *base_conditions
        )
    )
    total_seconds = 0
    for started_at, completed_at, print_time_seconds in archives_for_time.all():
        if started_at and completed_at:
            # Use actual elapsed time
            actual_seconds = (completed_at - started_at).total_seconds()
            if actual_seconds > 0:
                total_seconds += actual_seconds
        elif print_time_seconds:
            # Fallback to estimate only if no timestamps
            total_seconds += print_time_seconds
    total_time = total_seconds / 3600  # Convert to hours

    # Sum filament directly - filament_used_grams already contains the total for the print job
    filament_result = await db.execute(
        select(func.coalesce(func.sum(PrintArchive.filament_used_grams), 0)).where(*base_conditions)
    )
    total_filament = filament_result.scalar() or 0

    cost_result = await db.execute(select(func.sum(PrintArchive.cost)).where(*base_conditions))
    total_cost = cost_result.scalar() or 0

    # By filament type (split comma-separated values for multi-material prints)
    filament_type_result = await db.execute(
        select(PrintArchive.filament_type).where(PrintArchive.filament_type.isnot(None), *base_conditions)
    )
    prints_by_filament: dict[str, int] = {}
    for (filament_types,) in filament_type_result.all():
        # Split by comma and count each type
        for ftype in filament_types.split(","):
            ftype = ftype.strip()
            if ftype:
                prints_by_filament[ftype] = prints_by_filament.get(ftype, 0) + 1

    # By printer
    printer_result = await db.execute(
        select(PrintArchive.printer_id, func.count(PrintArchive.id))
        .where(*base_conditions)
        .group_by(PrintArchive.printer_id)
    )
    prints_by_printer = {str(k): v for k, v in printer_result.all()}

    # Time accuracy statistics
    # Get all completed archives with both estimated and actual times
    accuracy_result = await db.execute(
        select(PrintArchive)
        .where(PrintArchive.status == "completed", *base_conditions)
        .where(PrintArchive.print_time_seconds.isnot(None))
        .where(PrintArchive.started_at.isnot(None))
        .where(PrintArchive.completed_at.isnot(None))
    )
    archives_with_times = list(accuracy_result.scalars().all())

    average_accuracy = None
    accuracy_by_printer: dict[str, float] = {}

    if archives_with_times:
        accuracies = []
        printer_accuracies: dict[str, list[float]] = {}

        for archive in archives_with_times:
            acc_data = compute_time_accuracy(archive)
            if acc_data["time_accuracy"] is not None:
                accuracies.append(acc_data["time_accuracy"])

                # Group by printer
                printer_key = str(archive.printer_id) if archive.printer_id else "unknown"
                if printer_key not in printer_accuracies:
                    printer_accuracies[printer_key] = []
                printer_accuracies[printer_key].append(acc_data["time_accuracy"])

        if accuracies:
            average_accuracy = round(sum(accuracies) / len(accuracies), 1)

        # Calculate per-printer averages
        for printer_key, accs in printer_accuracies.items():
            accuracy_by_printer[printer_key] = round(sum(accs) / len(accs), 1)

    # Energy totals - check which mode to use
    from backend.app.api.routes.settings import get_setting

    energy_tracking_mode = await get_setting(db, "energy_tracking_mode") or "total"
    energy_cost_per_kwh_str = await get_setting(db, "energy_cost_per_kwh")
    energy_cost_per_kwh = float(energy_cost_per_kwh_str) if energy_cost_per_kwh_str else 0.15

    total_energy_kwh: float = 0.0
    total_energy_cost: float = 0.0
    energy_data_warming_up = False

    if energy_tracking_mode == "total" and not date_from and not date_to:
        # All-time total consumption - read live lifetime counters.
        total_energy_kwh = await _sum_live_plug_totals(db)
        total_energy_cost = total_energy_kwh * energy_cost_per_kwh
    elif energy_tracking_mode == "total":
        # Total consumption mode with a date filter (#941): use hourly snapshots
        # to compute per-plug (endpoint - baseline) deltas.
        from datetime import time as _time

        total_energy_kwh, energy_data_warming_up = await _sum_snapshot_deltas(
            db,
            dt_from=(datetime.combine(date_from, _time.min, tzinfo=timezone.utc) if date_from else None),
            dt_to=(datetime.combine(date_to, _time.max, tzinfo=timezone.utc) if date_to else None),
        )
        total_energy_cost = total_energy_kwh * energy_cost_per_kwh
    else:
        # Per-print mode: sum the per-print energy column directly.
        energy_kwh_result = await db.execute(select(func.sum(PrintArchive.energy_kwh)).where(*base_conditions))
        total_energy_kwh = energy_kwh_result.scalar() or 0

        energy_cost_result = await db.execute(select(func.sum(PrintArchive.energy_cost)).where(*base_conditions))
        total_energy_cost = energy_cost_result.scalar() or 0

    return ArchiveStats(
        total_prints=total_prints,
        successful_prints=successful_prints,
        failed_prints=failed_prints,
        total_print_time_hours=round(total_time, 1),
        total_filament_grams=round(total_filament, 1),
        total_cost=round(total_cost, 2),
        prints_by_filament_type=prints_by_filament,
        prints_by_printer=prints_by_printer,
        average_time_accuracy=average_accuracy,
        time_accuracy_by_printer=accuracy_by_printer if accuracy_by_printer else None,
        total_energy_kwh=round(total_energy_kwh, 3),
        total_energy_cost=round(total_energy_cost, 3),
        energy_data_warming_up=energy_data_warming_up,
    )


async def _sum_live_plug_totals(db: AsyncSession) -> float:
    """Sum the live lifetime counter from every smart plug.

    Used for all-time "total consumption" mode. Only the current value is
    available so this can't be date-filtered - use `_sum_snapshot_deltas` for
    that case.
    """
    from backend.app.api.routes.settings import get_setting
    from backend.app.models.smart_plug import SmartPlug
    from backend.app.services.homeassistant import homeassistant_service
    from backend.app.services.mqtt_relay import mqtt_relay
    from backend.app.services.rest_smart_plug import rest_smart_plug_service
    from backend.app.services.tasmota import tasmota_service

    plugs_result = await db.execute(select(SmartPlug))
    plugs = list(plugs_result.scalars().all())

    ha_url = await get_setting(db, "ha_url") or ""
    ha_token = await get_setting(db, "ha_token") or ""
    homeassistant_service.configure(ha_url, ha_token)

    total = 0.0
    for plug in plugs:
        if plug.plug_type == "tasmota":
            energy = await tasmota_service.get_energy(plug)
            if energy and energy.get("total") is not None:
                total += energy["total"]
        elif plug.plug_type == "homeassistant":
            energy = await homeassistant_service.get_energy(plug)
            if energy and energy.get("total") is not None:
                total += energy["total"]
        elif plug.plug_type == "mqtt":
            # MQTT plugs only expose today's counter, not lifetime.
            mqtt_data = mqtt_relay.smart_plug_service.get_plug_data(plug.id)
            if mqtt_data and mqtt_data.energy is not None:
                total += mqtt_data.energy
        elif plug.plug_type == "rest":
            energy = await rest_smart_plug_service.get_energy(plug)
            if energy and energy.get("today") is not None:
                total += energy["today"]
    return total


async def _sum_snapshot_deltas(
    db: AsyncSession,
    *,
    dt_from: datetime | None,
    dt_to: datetime | None,
) -> tuple[float, bool]:
    """Sum per-plug energy consumption over a date range using hourly snapshots.

    For each plug:
      * baseline  = last snapshot at or before `dt_from` (ideal)
                    - if missing, fall back to the earliest snapshot ever
                      recorded for the plug and flag the result as warming up.
      * endpoint  = last snapshot at or before `dt_to` (or most recent overall)
      * delta     = max(0, endpoint - baseline)  - clamp counter resets to 0.

    Returns (total_kwh, warming_up). `warming_up = True` means at least one plug
    had no baseline before `dt_from` (fresh install or fresh upgrade), so the
    result undercounts the beginning of the range.
    """
    from backend.app.models.smart_plug import SmartPlug
    from backend.app.models.smart_plug_energy_snapshot import SmartPlugEnergySnapshot

    plug_ids_result = await db.execute(select(SmartPlug.id))
    plug_ids = [row[0] for row in plug_ids_result.all()]
    if not plug_ids:
        return 0.0, False

    total = 0.0
    warming_up = False
    for plug_id in plug_ids:
        baseline: float | None = None
        if dt_from is not None:
            baseline_q = await db.execute(
                select(SmartPlugEnergySnapshot.lifetime_kwh)
                .where(
                    SmartPlugEnergySnapshot.plug_id == plug_id,
                    SmartPlugEnergySnapshot.recorded_at <= dt_from,
                )
                .order_by(SmartPlugEnergySnapshot.recorded_at.desc())
                .limit(1)
            )
            baseline = baseline_q.scalar()
        if baseline is None:
            # No snapshot before range start - fall back to the earliest
            # snapshot ever recorded. Result undercounts the pre-first-snapshot
            # portion of the range; signal that to the frontend.
            earliest_q = await db.execute(
                select(SmartPlugEnergySnapshot.lifetime_kwh)
                .where(SmartPlugEnergySnapshot.plug_id == plug_id)
                .order_by(SmartPlugEnergySnapshot.recorded_at.asc())
                .limit(1)
            )
            baseline = earliest_q.scalar()
            if baseline is None:
                # No snapshots at all for this plug yet.
                warming_up = True
                continue
            warming_up = True

        endpoint_conditions = [SmartPlugEnergySnapshot.plug_id == plug_id]
        if dt_to is not None:
            endpoint_conditions.append(SmartPlugEnergySnapshot.recorded_at <= dt_to)
        endpoint_q = await db.execute(
            select(SmartPlugEnergySnapshot.lifetime_kwh)
            .where(*endpoint_conditions)
            .order_by(SmartPlugEnergySnapshot.recorded_at.desc())
            .limit(1)
        )
        endpoint = endpoint_q.scalar()
        if endpoint is None:
            continue

        total += max(0.0, endpoint - baseline)

    return total, warming_up


@router.get("/tags")
async def get_all_tags(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """List all unique tags with usage counts.

    Returns a list of tags sorted by count (descending), then by name.
    """
    # Query all archives with non-null tags
    result = await db.execute(select(PrintArchive.tags).where(PrintArchive.tags.isnot(None)))
    all_tags_rows = result.all()

    # Count occurrences of each tag
    tag_counts: dict[str, int] = {}
    for (tags_str,) in all_tags_rows:
        if tags_str:
            for tag in tags_str.split(","):
                tag = tag.strip()
                if tag:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

    # Convert to list and sort by count (desc), then name (asc)
    tags_list = [{"name": name, "count": count} for name, count in tag_counts.items()]
    tags_list.sort(key=lambda x: (-x["count"], x["name"].lower()))

    return tags_list


@router.put("/tags/{tag_name}")
async def rename_tag(
    tag_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_ALL),
):
    """Rename a tag across all archives.

    Request body should contain {"new_name": "new tag name"}.
    Returns the count of affected archives.
    """
    body = await request.json()
    new_name = body.get("new_name", "").strip()

    if not new_name:
        raise HTTPException(400, "new_name is required")

    if new_name == tag_name:
        return {"affected": 0}

    # Find all archives containing the old tag
    result = await db.execute(select(PrintArchive).where(PrintArchive.tags.isnot(None)))
    archives = list(result.scalars().all())

    affected = 0
    for archive in archives:
        if not archive.tags:
            continue
        tags = [t.strip() for t in archive.tags.split(",")]
        if tag_name in tags:
            # Replace old tag with new tag
            new_tags = [new_name if t == tag_name else t for t in tags]
            # Remove duplicates while preserving order
            seen = set()
            unique_tags = []
            for t in new_tags:
                if t not in seen:
                    seen.add(t)
                    unique_tags.append(t)
            archive.tags = ", ".join(unique_tags)
            affected += 1

    await db.commit()
    return {"affected": affected}


@router.delete("/tags/{tag_name}")
async def delete_tag(
    tag_name: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_ALL),
):
    """Delete a tag from all archives.

    Returns the count of affected archives.
    """
    # Find all archives containing the tag
    result = await db.execute(select(PrintArchive).where(PrintArchive.tags.isnot(None)))
    archives = list(result.scalars().all())

    affected = 0
    for archive in archives:
        if not archive.tags:
            continue
        tags = [t.strip() for t in archive.tags.split(",")]
        if tag_name in tags:
            # Remove the tag
            new_tags = [t for t in tags if t != tag_name]
            archive.tags = ", ".join(new_tags) if new_tags else None
            affected += 1

    await db.commit()
    return {"affected": affected}


@router.get("/{archive_id}", response_model=ArchiveResponse)
async def get_archive(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Get a specific archive."""
    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    # Find duplicates
    makerworld_id = archive.extra_data.get("makerworld_model_id") if archive.extra_data else None
    # Pass effective hash (source_content_hash ?? content_hash) so chain
    # siblings (patched variants of the same source) are matched as duplicates.
    duplicates = await service.find_duplicates(
        archive_id=archive.id,
        content_hash=archive.source_content_hash or archive.content_hash,
        print_name=archive.print_name,
        makerworld_model_id=makerworld_id,
    )
    return archive_to_response(archive, duplicates)


@router.get("/{archive_id}/similar")
async def find_similar_archives(
    archive_id: int,
    limit: int = 10,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Find archives with similar settings for comparison.

    Returns archives that match by:
    - Same print name (highest priority)
    - Same file content hash
    - Same filament type
    """
    from backend.app.services.archive_comparison import ArchiveComparisonService

    service = ArchiveComparisonService(db)
    try:
        return await service.find_similar_archives(archive_id, limit=limit)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.patch("/{archive_id}", response_model=ArchiveResponse)
async def update_archive(
    archive_id: int,
    update_data: ArchiveUpdate,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.ARCHIVES_UPDATE_ALL,
            Permission.ARCHIVES_UPDATE_OWN,
        )
    ),
):
    """Update archive metadata (tags, notes, cost, is_favorite, project_id)."""
    from sqlalchemy.orm import selectinload

    user, can_modify_all = auth_result

    result = await db.execute(
        select(PrintArchive)
        .options(selectinload(PrintArchive.project), selectinload(PrintArchive.created_by))
        .where(PrintArchive.id == archive_id)
    )
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    # Ownership check
    if not can_modify_all:
        if archive.created_by_id != user.id:
            raise HTTPException(403, "You can only update your own archives")

    for field, value in update_data.model_dump(exclude_unset=True).items():
        setattr(archive, field, value)

    await db.commit()

    # Re-fetch with relationships loaded after commit
    result = await db.execute(
        select(PrintArchive)
        .options(selectinload(PrintArchive.project), selectinload(PrintArchive.created_by))
        .where(PrintArchive.id == archive_id)
    )
    archive = result.scalar_one_or_none()

    return archive_to_response(archive)


@router.post("/{archive_id}/favorite", response_model=ArchiveResponse)
async def toggle_favorite(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_OWN),
):
    """Toggle favorite status for an archive."""
    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    archive.is_favorite = not archive.is_favorite
    await db.commit()
    await db.refresh(archive)
    return archive


@router.post("/{archive_id}/retry-download")
async def retry_archive_download(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_ALL),
):
    """Manually trigger a 3MF download attempt for a fallback archive.

    Use case: the initial + startup + connect + last-chance retries all
    failed, leaving the archive with ``file_path=""``.  The user can
    click a button in the UI to try one more time — useful if they
    manually copied the file back to SD or the printer's FTP recovered.
    """
    from backend.app.services.archive_download_retry import archive_download_retry

    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")
    if archive.file_path:
        return {"status": "already_has_file", "recovered": False, "message": "Archive already has a file"}

    status = await archive_download_retry.retry_archive(archive_id)
    # Map service status → user-facing response.
    messages = {
        "recovered": "3MF recovered and attached",
        "already_has_file": "Archive already has a file",
        "in_progress": "Another retry is already running for this archive — please wait",
        "failed": "Download failed — printer FTP unreachable or file no longer on SD",
        "error": "Unexpected error — check server logs",
    }
    return {
        "status": status,
        "recovered": status == "recovered",
        "message": messages.get(status, "Unknown status"),
    }


@router.post("/{archive_id}/rescan", response_model=ArchiveResponse)
async def rescan_archive(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_ALL),
):
    """Rescan the 3MF file and update metadata."""
    from backend.app.api.routes.settings import get_setting
    from backend.app.services.archive import ThreeMFParser

    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    file_path = settings.base_dir / archive.file_path
    if not file_path.is_file():
        raise HTTPException(404, "Archive file not found")

    # Parse the 3MF file
    parser = ThreeMFParser(file_path)
    metadata = parser.parse()

    # Update fields from metadata
    if metadata.get("filament_type"):
        archive.filament_type = metadata["filament_type"]
    if metadata.get("filament_color"):
        archive.filament_color = metadata["filament_color"]
    if metadata.get("print_time_seconds"):
        archive.print_time_seconds = metadata["print_time_seconds"]
    if metadata.get("filament_used_grams"):
        archive.filament_used_grams = metadata["filament_used_grams"]
    if metadata.get("layer_height"):
        archive.layer_height = metadata["layer_height"]
    if metadata.get("nozzle_diameter"):
        archive.nozzle_diameter = metadata["nozzle_diameter"]
    if metadata.get("bed_temperature"):
        archive.bed_temperature = metadata["bed_temperature"]
    if metadata.get("nozzle_temperature"):
        archive.nozzle_temperature = metadata["nozzle_temperature"]
    if metadata.get("makerworld_url"):
        archive.makerworld_url = metadata["makerworld_url"]
    if metadata.get("designer"):
        archive.designer = metadata["designer"]

    # Calculate cost: prefer spool usage history, fallback to default setting

    if archive.filament_used_grams and archive.filament_type:
        usage_result = await db.execute(
            select(func.sum(SpoolUsageHistory.cost)).where(SpoolUsageHistory.archive_id == archive.id)
        )
        usage_cost = usage_result.scalar()
        if usage_cost is not None and usage_cost > 0:
            archive.cost = float(Decimal(str(usage_cost)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        else:
            default_cost_setting = await get_setting(db, "default_filament_cost")
            default_cost_per_kg = float(default_cost_setting) if default_cost_setting else 25.0
            archive.cost = float(
                Decimal(str((archive.filament_used_grams / 1000) * default_cost_per_kg)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )

    await db.commit()
    await db.refresh(archive)
    return archive


@router.post("/recalculate-costs")
async def recalculate_all_costs(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_ALL),
):
    """Recalculate costs for all archives based on filament usage and prices."""

    from backend.app.api.routes.settings import get_setting

    result = await db.execute(select(PrintArchive))
    archives = list(result.scalars().all())

    # Get default filament cost from settings
    default_cost_setting = await get_setting(db, "default_filament_cost")
    default_cost_per_kg = float(default_cost_setting) if default_cost_setting else 25.0

    # Pre-fetch all usage costs by archive_id
    usage_costs_result = await db.execute(
        select(SpoolUsageHistory.archive_id, func.sum(SpoolUsageHistory.cost)).group_by(SpoolUsageHistory.archive_id)
    )
    usage_costs = usage_costs_result.fetchall()
    cost_map = {row[0]: row[1] for row in usage_costs if row[0] is not None and row[1] is not None and row[1] > 0}

    updated = 0
    for archive in archives:
        usage_cost = cost_map.get(archive.id)
        if usage_cost is not None:
            new_cost = round(usage_cost, 2)
        else:
            # Fallback: sum costs for old records by print_name
            usage_result = await db.execute(
                select(func.sum(SpoolUsageHistory.cost)).where(
                    SpoolUsageHistory.print_name == archive.print_name,
                    SpoolUsageHistory.archive_id.is_(None),
                )
            )
            fallback_cost = usage_result.scalar()
            if fallback_cost is not None and fallback_cost > 0:
                new_cost = round(fallback_cost, 2)
            elif archive.filament_used_grams:
                new_cost = round((archive.filament_used_grams / 1000) * default_cost_per_kg, 2)
            else:
                new_cost = None
        if new_cost is not None and archive.cost != new_cost:
            archive.cost = new_cost
            updated += 1

    await db.commit()
    return {"message": f"Recalculated costs for {updated} archives", "updated": updated}


@router.post("/rescan-all")
async def rescan_all_archives(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_ALL),
):
    """Rescan all archives and update their metadata."""
    from backend.app.services.archive import ThreeMFParser

    result = await db.execute(select(PrintArchive))
    archives = list(result.scalars().all())

    updated = 0
    errors = []

    for archive in archives:
        try:
            file_path = settings.base_dir / archive.file_path
            if not file_path.is_file():
                errors.append({"id": archive.id, "error": "File not found"})
                continue

            parser = ThreeMFParser(file_path)
            metadata = parser.parse()

            if metadata.get("filament_type"):
                archive.filament_type = metadata["filament_type"]
            if metadata.get("filament_color"):
                archive.filament_color = metadata["filament_color"]
            if metadata.get("print_time_seconds"):
                archive.print_time_seconds = metadata["print_time_seconds"]
            if metadata.get("filament_used_grams"):
                archive.filament_used_grams = metadata["filament_used_grams"]
            if metadata.get("layer_height"):
                archive.layer_height = metadata["layer_height"]
            if metadata.get("nozzle_diameter"):
                archive.nozzle_diameter = metadata["nozzle_diameter"]
            if metadata.get("makerworld_url"):
                archive.makerworld_url = metadata["makerworld_url"]
            if metadata.get("designer"):
                archive.designer = metadata["designer"]

            updated += 1
        except Exception as e:
            logger.exception("Failed to rescan archive %s: %s", archive.id, e)
            errors.append({"id": archive.id, "error": "Failed to parse 3MF file"})

    await db.commit()
    return {"updated": updated, "errors": errors}


@router.get("/{archive_id}/duplicates")
async def get_archive_duplicates(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Get duplicates for a specific archive."""
    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    makerworld_id = archive.extra_data.get("makerworld_model_id") if archive.extra_data else None
    # Pass effective hash (source_content_hash ?? content_hash) so chain
    # siblings (patched variants of the same source) are matched as duplicates.
    duplicates = await service.find_duplicates(
        archive_id=archive.id,
        content_hash=archive.source_content_hash or archive.content_hash,
        print_name=archive.print_name,
        makerworld_model_id=makerworld_id,
    )
    return {"duplicates": duplicates, "count": len(duplicates)}


@router.post("/backfill-hashes")
async def backfill_content_hashes(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_ALL),
):
    """Compute and store content hashes for all archives missing them."""
    result = await db.execute(select(PrintArchive).where(PrintArchive.content_hash.is_(None)))
    archives = list(result.scalars().all())

    updated = 0
    errors = []

    for archive in archives:
        try:
            file_path = settings.base_dir / archive.file_path
            if not file_path.is_file():
                errors.append({"id": archive.id, "error": "File not found"})
                continue

            archive.content_hash = ArchiveService.compute_file_hash(file_path)
            updated += 1
        except Exception as e:
            logger.exception("Failed to compute hash for archive %s: %s", archive.id, e)
            errors.append({"id": archive.id, "error": "Failed to compute hash"})

    await db.commit()
    return {"updated": updated, "errors": errors}


@router.delete("/{archive_id}")
async def delete_archive(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.ARCHIVES_DELETE_ALL,
            Permission.ARCHIVES_DELETE_OWN,
        )
    ),
):
    """Soft-delete an archive (moves to the archive trash bin).

    Stamps ``deleted_at`` and returns ``trashed=True``. Sweeper hard-deletes
    after retention; users / admins can also restore from the trash UI or
    hard-delete-now to bypass the window.
    """
    from backend.app.services.archive_purge import archive_purge_service

    user, can_modify_all = auth_result

    # Only operate on active archives — re-deleting a trashed row is a no-op.
    result = await db.execute(PrintArchive.active().where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    if not can_modify_all:
        if archive.created_by_id != user.id:
            raise HTTPException(403, "You can only delete your own archives")

    await archive_purge_service.move_to_trash(db, archive)
    return {"status": "trashed", "trashed": True, "id": archive.id}


@router.get("/{archive_id}/download")
async def download_archive(
    archive_id: int,
    inline: bool = False,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Download the 3MF file."""
    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    file_path = settings.base_dir / archive.file_path
    if not file_path.is_file():
        raise HTTPException(404, "File not found")

    # Use inline disposition to let browser/OS handle file association
    content_disposition = "inline" if inline else "attachment"

    return FileResponse(
        path=file_path,
        filename=archive.filename,
        media_type="application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
        content_disposition_type=content_disposition,
    )


@router.get("/{archive_id}/file/{filename}")
async def download_archive_with_filename(
    archive_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Download the 3MF file with filename in URL."""
    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    file_path = settings.base_dir / archive.file_path
    if not file_path.is_file():
        raise HTTPException(404, "File not found")

    return FileResponse(
        path=file_path,
        filename=archive.filename,
        media_type="application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
    )


@router.post("/{archive_id}/slicer-token")
async def create_archive_slicer_token(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Create a short-lived download token for opening files in slicer applications.

    Slicer protocol handlers (bambustudioopen://, orcaslicer://) cannot send
    auth headers, so they use this token in the URL path instead.
    """
    from backend.app.core.auth import create_slicer_download_token

    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    token = await create_slicer_download_token("archive", archive_id)
    return {"token": token}


@router.get("/{archive_id}/dl/{token}/{filename}")
async def download_archive_for_slicer(
    archive_id: int,
    token: str,
    filename: str,
    db: AsyncSession = Depends(get_db),
):
    """Download 3MF file using a slicer download token.

    Token-authenticated (no auth headers needed). The token is short-lived
    and single-use, created by POST /{archive_id}/slicer-token.
    Filename is at the end of the URL so slicers can detect the file format.
    """
    from backend.app.core.auth import verify_slicer_download_token

    if not await verify_slicer_download_token(token, "archive", archive_id):
        raise HTTPException(403, "Invalid or expired download token")

    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    file_path = settings.base_dir / archive.file_path
    if not file_path.is_file():
        raise HTTPException(404, "File not found")

    return FileResponse(
        path=file_path,
        filename=archive.filename,
        media_type="application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
    )


@router.get("/{archive_id}/thumbnail")
async def get_thumbnail(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get the thumbnail image.

    Note: Unauthenticated - loaded via <img> tags which can't send auth headers.

    Trashed archives are intentionally accessible here so the trash UI can
    render previews next to the filename. The metadata is already exposed
    via the trash listing endpoint, so a thumbnail-only access leak is a
    no-op surface — the trashed row is otherwise visible to anyone the
    listing serves.
    """
    service = ArchiveService(db)
    archive = await service.get_archive(archive_id, include_trashed=True)
    if not archive or not archive.thumbnail_path:
        raise HTTPException(404, "Thumbnail not found")

    thumb_path = settings.base_dir / archive.thumbnail_path
    if not thumb_path.exists():
        raise HTTPException(404, "Thumbnail file not found")

    # Use file modification time as ETag to bust cache
    mtime = int(thumb_path.stat().st_mtime)

    return FileResponse(
        path=thumb_path,
        media_type="image/png",
        headers={
            "Cache-Control": "no-cache, must-revalidate",
            "ETag": f'"{mtime}"',
        },
    )


@router.get("/{archive_id}/timelapse")
async def get_timelapse(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get the timelapse video.

    Note: Unauthenticated - loaded via <video> tags which can't send auth headers.
    """
    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive or not archive.timelapse_path:
        raise HTTPException(404, "Timelapse not found")

    timelapse_path = settings.base_dir / archive.timelapse_path
    if not timelapse_path.exists():
        raise HTTPException(404, "Timelapse file not found")

    # Use file modification time as ETag to bust cache after processing
    mtime = int(timelapse_path.stat().st_mtime)

    # Detect media type from file extension (AVI from P1S before background conversion)
    suffix = timelapse_path.suffix.lower()
    media_type = {".mp4": "video/mp4", ".avi": "video/x-msvideo", ".mkv": "video/x-matroska"}.get(suffix, "video/mp4")
    ext = suffix if suffix in (".mp4", ".avi", ".mkv") else ".mp4"

    return FileResponse(
        path=timelapse_path,
        media_type=media_type,
        filename=f"{archive.print_name or 'timelapse'}{ext}",
        headers={
            "Cache-Control": "no-cache, must-revalidate",
            "ETag": f'"{mtime}"',
        },
    )


@router.delete("/{archive_id}/timelapse")
async def delete_timelapse(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_DELETE_OWN),
):
    """Remove the timelapse video from an archive."""
    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    if not archive.timelapse_path:
        raise HTTPException(404, "No timelapse attached to this archive")

    # Delete the file
    timelapse_path = settings.base_dir / archive.timelapse_path
    if timelapse_path.exists():
        timelapse_path.unlink()

    # Clear the path in database
    archive.timelapse_path = None
    await db.commit()

    return {"status": "deleted"}


@router.post("/{archive_id}/timelapse/scan")
async def scan_timelapse(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_ALL),
):
    """Scan printer for timelapse matching this archive and attach it."""
    from backend.app.models.printer import Printer
    from backend.app.services.bambu_ftp import (
        download_file_bytes_async,
        get_ftp_retry_settings,
        list_files_async,
        with_ftp_retry,
    )

    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    if archive.timelapse_path:
        return {"status": "exists", "message": "Timelapse already attached"}

    if not archive.printer_id:
        raise HTTPException(400, "Archive has no associated printer")

    # Get printer
    result = await db.execute(select(Printer).where(Printer.id == archive.printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    # Get base name from archive filename. `resolve_display_stem` (#1152) strips
    # the full `.gcode.3mf` double-suffix Bambu Studio uses by default — without
    # it `Path(...).stem` leaves `Plate_1.gcode` which fails the substring
    # match below against `Plate_1.mp4`-shaped timelapse names on the SD card.
    base_name = resolve_display_stem(archive.filename or "")

    # Scan timelapse directory on printer
    # Different printer models use different paths
    files = []
    for timelapse_path in ["/timelapse", "/timelapse/video", "/record", "/recording"]:
        try:
            files = await list_files_async(
                printer.ip_address, printer.access_code, timelapse_path, printer_model=printer.model
            )
            if files:
                break
        except Exception:
            continue
    if not files:
        raise HTTPException(500, "Failed to connect to printer or no timelapse directory found")

    # Look for matching timelapse
    matching_file = None
    video_files = [
        f for f in files if not f.get("is_directory") and f.get("name", "").lower().endswith((".mp4", ".avi"))
    ]

    # Strategy 1: Match by print name in filename
    for f in video_files:
        fname = f.get("name", "")
        if base_name.lower() in fname.lower():
            matching_file = f
            break

    # Strategy 2: Match by timestamp proximity
    # Bambu timelapse filename uses the print START time (when recording began)
    if not matching_file and (archive.started_at or archive.completed_at or archive.created_at):
        import re
        from datetime import datetime, timedelta

        # Prefer started_at since video filename is the print start time
        # Fall back to completed_at or created_at if started_at is not available
        archive_start = archive.started_at
        archive_end = archive.completed_at or archive.created_at
        best_match = None
        best_diff = timedelta(hours=24)  # Max 24 hour difference

        for f in video_files:
            fname = f.get("name", "")
            # Parse timestamp from filename like "video_2025-11-24_03-17-40.mp4"
            match = re.search(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})", fname)
            if match:
                try:
                    file_time = datetime.strptime(match.group(1), "%Y-%m-%d_%H-%M-%S")

                    # Try multiple timezone offsets since printer timezone can vary
                    # Common cases: local time (0), CST/UTC+8 (+8), or UTC (-local offset)
                    for hour_offset in [0, 8, -8, 7, -7, 1, -1]:
                        adjusted_file_time = file_time - timedelta(hours=hour_offset)

                        # Check against start time (video filename = print start)
                        if archive_start:
                            diff = abs(adjusted_file_time - archive_start)
                            if diff < best_diff:
                                best_diff = diff
                                best_match = f
                                logger.debug(
                                    f"Timelapse match candidate: {fname} with offset {hour_offset}h, "
                                    f"diff from start: {diff}"
                                )

                        # Also check against end time with a buffer
                        # (video timestamp should be BEFORE completion time)
                        if archive_end:
                            # The video timestamp should be within the print duration before completion
                            if adjusted_file_time < archive_end:
                                diff = archive_end - adjusted_file_time
                                # Reasonable print duration: up to 48 hours
                                if diff < timedelta(hours=48) and diff < best_diff:
                                    best_diff = diff
                                    best_match = f
                                    logger.debug(
                                        f"Timelapse match candidate (from end): {fname} with offset {hour_offset}h, "
                                        f"diff: {diff}"
                                    )

                except ValueError:
                    continue

        # Accept match within 4 hours (more lenient for timezone issues)
        if best_match and best_diff < timedelta(hours=4):
            matching_file = best_match
            logger.info("Matched timelapse by timestamp: %s (diff: %s)", best_match.get("name"), best_diff)

    # Strategy 3: Use file modification time from FTP listing
    # This handles cases where printer's filename timestamp is wrong but file mtime is correct
    if not matching_file and (archive.started_at or archive.completed_at or archive.created_at):
        from datetime import datetime, timedelta

        _archive_start = archive.started_at
        archive_end = archive.completed_at or archive.created_at
        best_match = None
        best_diff = timedelta(hours=24)

        for f in video_files:
            mtime = f.get("mtime")
            if mtime:
                # Timelapse file should be modified during or shortly after the print
                # The mtime should be close to completion time (video finishes when print ends)
                if archive_end:
                    diff = abs(mtime - archive_end)
                    if diff < best_diff:
                        best_diff = diff
                        best_match = f
                        logger.debug(
                            f"Timelapse mtime match candidate: {f.get('name')}, mtime: {mtime}, diff from end: {diff}"
                        )

        if best_match and best_diff < timedelta(hours=2):
            matching_file = best_match
            logger.info("Matched timelapse by file mtime: %s (diff: %s)", best_match.get("name"), best_diff)

    # Strategy 4: If only one timelapse exists and archive was recently completed, use it
    # This handles cases where printer clock is wrong or timezone issues exist
    if not matching_file and len(video_files) == 1:
        from datetime import datetime, timedelta, timezone

        archive_completed = archive.completed_at or archive.created_at
        if archive_completed:
            if archive_completed.tzinfo is None:
                archive_completed = archive_completed.replace(tzinfo=timezone.utc)
            time_since_completion = datetime.now(timezone.utc) - archive_completed
            # If archive was completed within the last hour, assume the single timelapse is for it
            if time_since_completion < timedelta(hours=1):
                matching_file = video_files[0]
                logger.info("Using single timelapse file as fallback: %s", video_files[0].get("name"))

    # Note: We intentionally don't use a "most recent file" fallback because
    # we can't verify if timelapse was actually enabled for this print.
    # Instead, return the list of available files for manual selection.

    if not matching_file:
        # Return available files for manual selection
        available_files = [
            {
                "name": f.get("name"),
                "path": f.get("path"),
                "size": f.get("size"),
                "mtime": f.get("mtime").isoformat() if f.get("mtime") else None,
            }
            for f in video_files
        ]
        # Sort by mtime descending (most recent first)
        available_files.sort(key=lambda x: x.get("mtime") or "", reverse=True)
        return {
            "status": "not_found",
            "message": "No matching timelapse found - please select manually",
            "available_files": available_files,
        }

    # Download the timelapse - use the full path from the file listing
    remote_path = matching_file.get("path") or f"/timelapse/{matching_file['name']}"

    # Get FTP retry settings
    ftp_retry_enabled, ftp_retry_count, ftp_retry_delay, ftp_timeout = await get_ftp_retry_settings()

    if ftp_retry_enabled:
        timelapse_data = await with_ftp_retry(
            download_file_bytes_async,
            printer.ip_address,
            printer.access_code,
            remote_path,
            socket_timeout=ftp_timeout,
            printer_model=printer.model,
            max_retries=ftp_retry_count,
            retry_delay=ftp_retry_delay,
            operation_name=f"Download timelapse {matching_file['name']}",
        )
    else:
        timelapse_data = await download_file_bytes_async(
            printer.ip_address,
            printer.access_code,
            remote_path,
            socket_timeout=ftp_timeout,
            printer_model=printer.model,
        )

    if not timelapse_data:
        raise HTTPException(500, "Failed to download timelapse")

    # Attach timelapse to archive
    success = await service.attach_timelapse(archive_id, timelapse_data, matching_file["name"])

    if not success:
        raise HTTPException(500, "Failed to attach timelapse")

    return {
        "status": "attached",
        "message": f"Timelapse '{matching_file['name']}' attached successfully",
        "filename": matching_file["name"],
    }


@router.post("/{archive_id}/timelapse/select")
async def select_timelapse(
    archive_id: int,
    filename: str = Query(..., description="Timelapse filename to attach"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_ALL),
):
    """Manually select a timelapse from the printer to attach."""
    from backend.app.models.printer import Printer
    from backend.app.services.bambu_ftp import (
        download_file_bytes_async,
        get_ftp_retry_settings,
        list_files_async,
        with_ftp_retry,
    )

    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    if not archive.printer_id:
        raise HTTPException(400, "Archive has no associated printer")

    result = await db.execute(select(Printer).where(Printer.id == archive.printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    # Find the file on the printer
    files = []
    remote_path = None
    for timelapse_dir in ["/timelapse", "/timelapse/video", "/record", "/recording"]:
        try:
            files = await list_files_async(
                printer.ip_address, printer.access_code, timelapse_dir, printer_model=printer.model
            )
            for f in files:
                if f.get("name") == filename:
                    remote_path = f.get("path") or f"{timelapse_dir}/{filename}"
                    break
            if remote_path:
                break
        except Exception:
            continue

    if not remote_path:
        raise HTTPException(404, f"Timelapse '{filename}' not found on printer")

    # Download and attach
    ftp_retry_enabled, ftp_retry_count, ftp_retry_delay, ftp_timeout = await get_ftp_retry_settings()

    if ftp_retry_enabled:
        timelapse_data = await with_ftp_retry(
            download_file_bytes_async,
            printer.ip_address,
            printer.access_code,
            remote_path,
            socket_timeout=ftp_timeout,
            printer_model=printer.model,
            max_retries=ftp_retry_count,
            retry_delay=ftp_retry_delay,
            operation_name=f"Download timelapse {filename}",
        )
    else:
        timelapse_data = await download_file_bytes_async(
            printer.ip_address,
            printer.access_code,
            remote_path,
            socket_timeout=ftp_timeout,
            printer_model=printer.model,
        )

    if not timelapse_data:
        raise HTTPException(500, "Failed to download timelapse")

    success = await service.attach_timelapse(archive_id, timelapse_data, filename)
    if not success:
        raise HTTPException(500, "Failed to attach timelapse")

    return {
        "status": "attached",
        "message": f"Timelapse '{filename}' attached successfully",
        "filename": filename,
    }


@router.post("/{archive_id}/timelapse/upload")
async def upload_timelapse(
    archive_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_ALL),
):
    """Manually upload a timelapse video to an archive."""
    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    if not file.filename or not file.filename.endswith((".mp4", ".avi", ".mkv")):
        raise HTTPException(400, "File must be a video file (.mp4, .avi, .mkv)")

    content = await file.read()
    safe_name = _safe_filename(file.filename)
    success = await service.attach_timelapse(archive_id, content, safe_name)

    if not success:
        raise HTTPException(500, "Failed to attach timelapse")

    return {"status": "attached", "filename": safe_name}


@router.get("/{archive_id}/timelapse/info")
async def get_timelapse_info(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Get timelapse video metadata for editor."""
    from backend.app.schemas.timelapse import TimelapseInfoResponse
    from backend.app.services.timelapse_processor import TimelapseProcessor

    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive or not archive.timelapse_path:
        raise HTTPException(404, "Timelapse not found")

    timelapse_path = settings.base_dir / archive.timelapse_path
    if not timelapse_path.exists():
        raise HTTPException(404, "Timelapse file not found")

    try:
        processor = TimelapseProcessor(timelapse_path)
        info = await processor.get_info()
        return TimelapseInfoResponse(**info)
    except Exception as e:
        logger.error("Failed to get timelapse info: %s", e)
        raise HTTPException(500, f"Failed to get video info: {str(e)}")


@router.get("/{archive_id}/timelapse/thumbnails")
async def get_timelapse_thumbnails(
    archive_id: int,
    count: int = Query(10, ge=1, le=30),
    width: int = Query(160, ge=80, le=320),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Generate timeline thumbnail frames for visual scrubbing."""
    import base64

    from backend.app.schemas.timelapse import ThumbnailResponse
    from backend.app.services.timelapse_processor import TimelapseProcessor

    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive or not archive.timelapse_path:
        raise HTTPException(404, "Timelapse not found")

    timelapse_path = settings.base_dir / archive.timelapse_path
    if not timelapse_path.exists():
        raise HTTPException(404, "Timelapse file not found")

    try:
        processor = TimelapseProcessor(timelapse_path)
        thumbnails = await processor.generate_thumbnails(count, width)

        return ThumbnailResponse(
            thumbnails=[base64.b64encode(data).decode() for _, data in thumbnails],
            timestamps=[ts for ts, _ in thumbnails],
        )
    except Exception as e:
        logger.error("Failed to generate thumbnails: %s", e)
        raise HTTPException(500, f"Failed to generate thumbnails: {str(e)}")


@router.post("/{archive_id}/timelapse/process")
async def process_timelapse(
    archive_id: int,
    trim_start: float = Form(0),
    trim_end: float = Form(None),
    speed: float = Form(1.0),
    save_mode: str = Form("new"),
    output_filename: str = Form(None),
    audio: UploadFile = File(None),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_ALL),
):
    """Process timelapse with trim, speed, and optional audio overlay."""
    import shutil
    import tempfile

    from backend.app.schemas.timelapse import ProcessResponse
    from backend.app.services.timelapse_processor import TimelapseProcessor

    # Validate speed
    if not 0.25 <= speed <= 4.0:
        raise HTTPException(400, "Speed must be between 0.25 and 4.0")

    if save_mode not in ("replace", "new"):
        raise HTTPException(400, "save_mode must be 'replace' or 'new'")

    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive or not archive.timelapse_path:
        raise HTTPException(404, "Timelapse not found")

    timelapse_path = settings.base_dir / archive.timelapse_path
    if not timelapse_path.exists():
        raise HTTPException(404, "Timelapse file not found")

    archive_dir = timelapse_path.parent

    # Handle audio file
    audio_temp_path = None
    if audio and audio.filename:
        # Validate audio file extension
        if not audio.filename.lower().endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg")):
            raise HTTPException(400, "Audio must be .mp3, .wav, .m4a, .aac, or .ogg")

        audio_content = await audio.read()
        # Extract and validate suffix to prevent path injection
        suffix = Path(audio.filename).suffix.lower()
        if suffix not in (".mp3", ".wav", ".m4a", ".aac", ".ogg"):
            raise HTTPException(400, "Invalid audio file extension")
        audio_temp_path = Path(tempfile.gettempdir()) / f"audio_{archive_id}{suffix}"
        audio_temp_path.write_bytes(audio_content)

    try:
        processor = TimelapseProcessor(timelapse_path)

        # Determine output path
        if save_mode == "replace":
            # Process to temp file first, then replace
            temp_output = Path(tempfile.gettempdir()) / f"processed_{archive_id}.mp4"
            output_path = temp_output
        else:
            # Save as new file alongside original
            filename = output_filename or f"{archive.print_name or 'timelapse'}_edited.mp4"
            # Sanitize filename - remove path separators and traversal sequences
            filename = "".join(c for c in filename if c.isalnum() or c in "._- ")
            # Prevent path traversal
            if ".." in filename or not filename or filename.startswith("."):
                filename = f"timelapse_{archive_id}_edited"
            if not filename.endswith(".mp4"):
                filename += ".mp4"
            output_path = archive_dir / filename

        success = await processor.process(
            output_path=output_path,
            trim_start=trim_start,
            trim_end=trim_end,
            speed=speed,
            audio_path=audio_temp_path,
        )

        if not success:
            raise HTTPException(500, "Video processing failed")

        # Handle save mode
        if save_mode == "replace":
            # Replace original file
            shutil.move(str(output_path), str(timelapse_path))
            final_path = archive.timelapse_path
            message = "Timelapse replaced successfully"
        else:
            final_path = str(output_path.relative_to(settings.base_dir))
            message = f"Saved as {output_path.name}"

        return ProcessResponse(
            status="completed",
            output_path=final_path,
            message=message,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Timelapse processing failed: %s", e)
        raise HTTPException(500, f"Processing failed: {str(e)}")
    finally:
        # Cleanup temp audio file
        if audio_temp_path and audio_temp_path.exists():
            audio_temp_path.unlink()


# ============================================
# Photo Endpoints
# ============================================


@router.post("/{archive_id}/photos")
async def upload_photo(
    archive_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_OWN),
):
    """Upload a photo of the printed result."""
    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    if not file.filename or not file.filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        raise HTTPException(400, "File must be an image (.jpg, .jpeg, .png, .webp)")

    # Get archive directory
    archive_dir = settings.base_dir / Path(archive.file_path).parent
    photos_dir = archive_dir / "photos"
    photos_dir.mkdir(exist_ok=True)

    # Generate unique filename
    import uuid

    ext = Path(_safe_filename(file.filename)).suffix.lower()
    photo_filename = f"{uuid.uuid4().hex[:8]}{ext}"
    photo_path = photos_dir / photo_filename

    # Save file
    content = await file.read()
    photo_path.write_bytes(content)

    # Update archive photos list (create new list to trigger SQLAlchemy change detection)
    photos = list(archive.photos or [])
    photos.append(photo_filename)
    archive.photos = photos

    await db.commit()
    await db.refresh(archive)

    return {"status": "uploaded", "filename": photo_filename, "photos": archive.photos}


@router.get("/{archive_id}/photos/{filename}")
async def get_photo(
    archive_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
):
    """Get a specific photo.

    Note: Unauthenticated - loaded via <img> tags which can't send auth headers.
    """
    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    archive_dir = settings.base_dir / Path(archive.file_path).parent
    photo_path = archive_dir / "photos" / filename

    if not photo_path.exists():
        raise HTTPException(404, "Photo not found")

    # Determine media type
    ext = Path(filename).suffix.lower()
    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    media_type = media_types.get(ext, "image/jpeg")

    return FileResponse(path=photo_path, media_type=media_type)


@router.delete("/{archive_id}/photos/{filename}")
async def delete_photo(
    archive_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_DELETE_OWN),
):
    """Delete a photo."""
    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    if not archive.photos or filename not in archive.photos:
        raise HTTPException(404, "Photo not found")

    # Delete file
    archive_dir = settings.base_dir / Path(archive.file_path).parent
    photo_path = archive_dir / "photos" / filename
    if photo_path.exists():
        photo_path.unlink()

    # Update archive photos list
    photos = [p for p in archive.photos if p != filename]
    archive.photos = photos if photos else None

    await db.commit()

    return {"status": "deleted", "photos": archive.photos}


# ============================================
# QR Code Endpoint
# ============================================


@router.get("/{archive_id}/qrcode")
async def get_qrcode(
    archive_id: int,
    request: Request,
    size: int = 200,
    db: AsyncSession = Depends(get_db),
):
    """Generate a QR code that links to this archive.

    Note: Unauthenticated - loaded via <img> tags which can't send auth headers.
    """
    try:
        import qrcode
        from PIL import Image as PILImage
    except ImportError:
        raise HTTPException(500, "QR code generation not available - qrcode package not installed")

    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    # Build URL to archive download
    base_url = str(request.base_url).rstrip("/")
    archive_url = f"{base_url}/api/v1/archives/{archive_id}/download"

    # Generate QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(archive_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    # Convert to PIL Image for resizing
    pil_img = img.get_image()

    # Resize if needed
    if size != 200:
        pil_img = pil_img.resize((size, size), PILImage.Resampling.LANCZOS)

    # Convert to bytes
    buffer = io.BytesIO()
    pil_img.save(buffer, format="PNG")
    buffer.seek(0)

    return Response(
        content=buffer.getvalue(),
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="qr_{archive.print_name or archive_id}.png"'},
    )


@router.get("/{archive_id}/capabilities")
async def get_archive_capabilities(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Check what viewing capabilities are available for this 3MF file."""
    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    file_path = settings.base_dir / archive.file_path
    if not file_path.is_file():
        raise HTTPException(404, "File not found")

    source_path: Path | None = None
    if archive.source_3mf_path:
        candidate = settings.base_dir / archive.source_3mf_path
        if candidate.exists():
            source_path = candidate

    try:
        caps = extract_3mf_capabilities(primary_path=file_path, source_path=source_path)
    except zipfile.BadZipFile as exc:
        raise HTTPException(400, "Invalid 3MF file") from exc

    # 3D-model tab is only meaningful when an unsliced source 3MF is
    # available and contains mesh data. The sliced container's embedded
    # mesh is already rasterised into the G-code preview, so re-rendering
    # it under "3D Model" duplicates information and confuses users —
    # they expect 3D to mean "the original model before slicing".
    has_model = caps.has_mesh_in_source

    return {
        "has_model": has_model,
        "has_gcode": caps.has_gcode,
        "has_source": source_path is not None,
        "build_volume": caps.build_volume,
        "filament_colors": caps.filament_colors,
    }


@router.get("/{archive_id}/gcode")
async def get_gcode(
    archive_id: int,
    plate: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Extract and return G-code from the 3MF file.

    Resolution order for which plate's gcode to serve:
    1. ``?plate=N`` query param — explicit caller-supplied plate. Must be
       ≥ 1; resolved by parsing the trailing integer in each
       ``Metadata/plate_<N>.gcode`` filename so zero-padded names
       (``plate_01.gcode``) match the canonical ``plate=1`` request.
    2. ``archive.plate_index`` (set at ``archive_print`` time, m038
       backfill for legacy rows) — what was actually printed.
    3. First ``Metadata/*.gcode`` entry — legacy single-plate fallback.

    A non-matching ``?plate=N`` returns 404 instead of falling through —
    the caller asked for a specific plate; surfacing the mismatch keeps
    the URL ↔ content contract honest. The ``archive.plate_index``
    fallback DOES forgive a missing match (legacy backfill rows had no
    way to know whether the container actually held that plate).
    """
    if plate is not None and plate < 1:
        raise HTTPException(400, "plate must be ≥ 1 (1-indexed)")

    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    file_path = settings.base_dir / archive.file_path
    if not file_path.is_file():
        raise HTTPException(404, "File not found")

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            # Bambu 3MF files store G-code in Metadata/plate_X.gcode
            gcode_files = [n for n in zf.namelist() if n.startswith("Metadata/") and n.endswith(".gcode")]
            if not gcode_files:
                raise HTTPException(
                    404,
                    "No G-code found. This file hasn't been sliced yet - G-code is only available after slicing in Bambu Studio.",
                )

            target_name: str | None = None
            if plate is not None:
                # Parse the trailing integer from each plate_N.gcode name so
                # zero-padded filenames (plate_01.gcode) still match plate=1.
                target_name = next(
                    (n for n in gcode_files if _plate_index_from_gcode_name(n) == plate),
                    None,
                )
                if target_name is None:
                    raise HTTPException(404, f"Plate {plate} not found in this archive")
            elif archive.plate_index is not None:
                expected_suffix = f"plate_{archive.plate_index}.gcode"
                target_name = next(
                    (n for n in gcode_files if n.lower().endswith(expected_suffix)),
                    None,
                )
                # Don't 404 the whole request just because the recorded
                # plate isn't in the container — fall back to the first
                # gcode so the user still sees something. Catches legacy
                # archives whose plate_index was backfilled from the name
                # suffix even though the container only has plate_1.
            if target_name is None:
                target_name = gcode_files[0]

            gcode_content = zf.read(target_name).decode("utf-8")
            return Response(content=gcode_content, media_type="text/plain")
    except zipfile.BadZipFile:
        raise HTTPException(400, "Invalid 3MF file")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error extracting G-code: {str(e)}")


def _plate_index_from_gcode_name(name: str) -> int | None:
    """Parse the integer plate index out of a ``Metadata/plate_<N>.gcode``
    name. Tolerates zero-padding and any case. Returns None for any name
    that doesn't match the expected shape.
    """
    import re as _re

    m = _re.search(r"plate_(\d+)\.gcode$", name, _re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


@router.get("/{archive_id}/plate-preview")
async def get_plate_preview(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get the plate preview image from the 3MF file.

    Returns the slicer-generated plate thumbnail which shows the model
    with correct colors and positioning.

    Note: Unauthenticated - loaded via <img> tags which can't send auth headers.
    """
    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    file_path = settings.base_dir / archive.file_path
    if not file_path.is_file():
        raise HTTPException(404, "File not found")

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            names = zf.namelist()

            # Try to find plate preview images in order of preference
            # First look for the specific plate being printed (check slice_info for plate index)
            plate_num = 1
            if "Metadata/slice_info.config" in names:
                try:
                    import defusedxml.ElementTree as ET

                    slice_content = zf.read("Metadata/slice_info.config").decode("utf-8")
                    root = ET.fromstring(slice_content)
                    plate_elem = root.find(".//plate/metadata[@key='index']")
                    if plate_elem is not None:
                        plate_num = int(plate_elem.get("value", "1"))
                except Exception:
                    pass  # Default plate_num=1 if slice_info is missing or malformed

            # Try plate-specific image first, then fall back to plate_1
            preview_paths = [
                f"Metadata/plate_{plate_num}.png",
                "Metadata/plate_1.png",
                "Metadata/thumbnail.png",
            ]

            for preview_path in preview_paths:
                if preview_path in names:
                    image_data = zf.read(preview_path)
                    return Response(content=image_data, media_type="image/png")

            # If no plate image, try any PNG in Metadata
            for name in names:
                if name.startswith("Metadata/plate_") and name.endswith(".png") and "_small" not in name:
                    image_data = zf.read(name)
                    return Response(content=image_data, media_type="image/png")

            raise HTTPException(404, "No plate preview found in 3MF file")

    except zipfile.BadZipFile:
        raise HTTPException(400, "Invalid 3MF file")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error extracting plate preview: {str(e)}")


@router.get("/{archive_id}/plates")
async def get_archive_plates(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Get available plates from a multi-plate 3MF archive.

    Returns a list of plates with their index, name, thumbnail availability,
    and filament requirements. For single-plate exports, returns a single plate.
    """

    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    file_path = settings.base_dir / archive.file_path
    if not file_path.is_file():
        raise HTTPException(404, "Archive file not found")

    # SliceModal pre-check signal: the source 3MF's bound printer model.
    source_printer_model: str | None = None
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            source_printer_model = extract_source_printer_model_from_3mf(zf)
    except (zipfile.BadZipFile, OSError):
        pass

    # Fast path: read pre-computed plates from the archive's JSON metadata
    # (populated at archive_print() / attach_3mf_to_archive() + by m023
    # backfill). No ZIP open.
    cached_plates = (archive.extra_data or {}).get("plates") if isinstance(archive.extra_data, dict) else None
    if isinstance(cached_plates, list) and cached_plates:
        plates = [
            {
                **p,
                "thumbnail_url": (
                    f"/api/v1/archives/{archive_id}/plate-thumbnail/{p.get('index')}"
                    if p.get("has_thumbnail")
                    else None
                ),
            }
            for p in cached_plates
        ]
        # has_gcode tracks whether the on-disk container actually carries
        # sliced gcode (not just plate PNG/JSON metadata). Source-only 3MFs
        # — pure project files exported from Bambu Studio without a slice —
        # have plates with thumbnails + filament info but no gcode payload,
        # so a viewer can't render anything for them. Frontend keys on this
        # flag to suppress the gcode tab / show a "no gcode" state instead.
        has_gcode = _archive_has_gcode(file_path)
        return {
            "archive_id": archive_id,
            "filename": archive.filename,
            "plates": plates,
            "is_multi_plate": len(plates) > 1,
            "has_gcode": has_gcode,
            "source_printer_model": source_printer_model,
        }

    # Slow path: open ZIP + parse. Used for archives created before m023 ran.
    plates: list[dict] = []
    has_gcode = False
    try:
        from backend.app.services.archive import parse_plates_from_3mf

        with zipfile.ZipFile(file_path, "r") as zf:
            raw_plates = parse_plates_from_3mf(zf)
            # Same semantic as the fast path — surface whether sliced gcode
            # is actually inside the container so the frontend can skip the
            # picker for source-only archives.
            has_gcode = any(n.startswith("Metadata/") and n.endswith(".gcode") for n in zf.namelist())
        for p in raw_plates:
            plates.append(
                {
                    **p,
                    "thumbnail_url": (
                        f"/api/v1/archives/{archive_id}/plate-thumbnail/{p['index']}" if p["has_thumbnail"] else None
                    ),
                }
            )
    except Exception as e:
        logger.warning("Failed to parse plates from archive %s: %s", archive_id, e)

    return {
        "archive_id": archive_id,
        "filename": archive.filename,
        "plates": plates,
        "is_multi_plate": len(plates) > 1,
        "has_gcode": has_gcode,
        "source_printer_model": source_printer_model,
    }


def _archive_has_gcode(file_path: Path) -> bool:
    """Quick boolean check — does the 3MF actually contain sliced gcode?

    The /plates fast path reads pre-computed plate JSON from extra_data
    without opening the ZIP, so we need a separate cheap probe to expose
    has_gcode without forcing the slow path to open the file twice.
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            return any(n.startswith("Metadata/") and n.endswith(".gcode") for n in zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


@router.get("/{archive_id}/plate-thumbnail/{plate_index}")
async def get_plate_thumbnail(
    archive_id: int,
    plate_index: int,
    db: AsyncSession = Depends(get_db),
):
    """Get the thumbnail image for a specific plate.

    Note: Unauthenticated - loaded via <img> tags which can't send auth headers.
    """
    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    file_path = settings.base_dir / archive.file_path
    if not file_path.is_file():
        raise HTTPException(404, "Archive file not found")

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            thumb_path = f"Metadata/plate_{plate_index}.png"
            if thumb_path in zf.namelist():
                data = zf.read(thumb_path)
                return Response(content=data, media_type="image/png")
    except Exception:
        pass  # Fall through to 404 if archive is unreadable or thumbnail missing

    raise HTTPException(404, f"Thumbnail for plate {plate_index} not found")


async def _try_preview_slice_filaments(
    db: AsyncSession,
    *,
    kind: str,
    source_id: int,
    plate_id: int,
    file_path: Path,
    request_id: str | None = None,
) -> list[dict] | None:
    """Run a preview slice via the user's configured sidecar so the filament
    list endpoint can return real per-plate filaments for unsliced project
    files. Returns ``None`` on any failure — the caller falls back to the
    project-config heuristic. ``request_id`` flows through to the sidecar
    for live progress on the SliceModal's inline spinner + toast.

    Always uses the global ``preferred_slicer`` setting -- the per-job
    slicer override on ``SliceRequest.slicer`` is consulted only by the
    real slice routes, not by the modal's filament-discovery preview.
    """
    from backend.app.services.slice_preview import get_preview_filaments
    from backend.app.services.slicer_routing import resolve_sidecar_url

    _, api_url = await resolve_sidecar_url(db)
    if not api_url:
        return None

    try:
        file_bytes = file_path.read_bytes()
    except OSError:
        return None
    return await get_preview_filaments(
        kind=kind,
        source_id=source_id,
        plate_id=plate_id,
        file_bytes=file_bytes,
        file_name=file_path.name,
        api_url=api_url,
        request_id=request_id,
    )


@router.get("/{archive_id}/filament-requirements")
async def get_filament_requirements(
    archive_id: int,
    plate_id: int | None = None,
    request_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Get filament requirements from the archived 3MF file.

    Returns the filaments used in this print with their slot IDs, types, colors,
    and usage amounts. This can be compared with current AMS state before reprinting.

    Args:
        archive_id: The archive ID
        plate_id: Optional plate index to filter filaments for (for multi-plate files)
        request_id: forwarded to the sidecar's preview-slice fallback for
            unsliced project files; lets the SliceModal poll matching live
            progress.
    """
    import defusedxml.ElementTree as ET

    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    file_path = settings.base_dir / archive.file_path
    if not file_path.is_file():
        raise HTTPException(404, "Archive file not found")

    filaments = []

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            # Parse slice_info.config for filament requirements
            if "Metadata/slice_info.config" in zf.namelist():
                content = zf.read("Metadata/slice_info.config").decode()
                root = ET.fromstring(content)

                # If plate_id is specified, find filaments for that specific plate
                if plate_id is not None:
                    # Find the plate element with matching index
                    for plate_elem in root.findall(".//plate"):
                        plate_index = None
                        for meta in plate_elem.findall("metadata"):
                            if meta.get("key") == "index":
                                try:
                                    plate_index = int(meta.get("value", "0"))
                                except ValueError:
                                    pass  # Skip plate with non-numeric index metadata
                                break

                        if plate_index == plate_id:
                            # Extract filaments from this plate element
                            for filament_elem in plate_elem.findall("filament"):
                                filament_id = filament_elem.get("id")
                                filament_type = filament_elem.get("type", "")
                                filament_color = filament_elem.get("color", "")
                                used_g = filament_elem.get("used_g", "0")
                                used_m = filament_elem.get("used_m", "0")

                                tray_info_idx = filament_elem.get("tray_info_idx", "")

                                try:
                                    used_grams = float(used_g)
                                except (ValueError, TypeError):
                                    used_grams = 0

                                if used_grams > 0 and filament_id:
                                    filaments.append(
                                        {
                                            "slot_id": int(filament_id),
                                            "type": filament_type,
                                            "color": filament_color,
                                            "used_grams": round(used_grams, 1),
                                            "used_meters": float(used_m) if used_m else 0,
                                            "tray_info_idx": tray_info_idx,
                                            "used_in_plate": True,
                                        }
                                    )
                            break
                else:
                    # No plate_id specified - extract all filaments with used_g > 0
                    # This is the legacy behavior for single-plate files
                    for filament_elem in root.findall(".//filament"):
                        filament_id = filament_elem.get("id")
                        filament_type = filament_elem.get("type", "")
                        filament_color = filament_elem.get("color", "")
                        used_g = filament_elem.get("used_g", "0")
                        used_m = filament_elem.get("used_m", "0")

                        tray_info_idx = filament_elem.get("tray_info_idx", "")

                        # Only include filaments that are actually used
                        try:
                            used_grams = float(used_g)
                        except (ValueError, TypeError):
                            used_grams = 0

                        if used_grams > 0 and filament_id:
                            filaments.append(
                                {
                                    "slot_id": int(filament_id),
                                    "type": filament_type,
                                    "color": filament_color,
                                    "used_grams": round(used_grams, 1),
                                    "used_meters": float(used_m) if used_m else 0,
                                    "tray_info_idx": tray_info_idx,
                                    "used_in_plate": True,
                                }
                            )

            # Unsliced project files: see library.py for full rationale.
            # Return the FULL project_settings.config slot list with a
            # used_in_plate flag derived from the preview slice; the
            # CLI needs every slot pre-filled to avoid silent default
            # substitution.
            if not filaments:
                project_filaments = extract_project_filaments_from_3mf(zf)
                used_slot_ids: set[int] = set()
                if project_filaments and plate_id is not None:
                    preview = await _try_preview_slice_filaments(
                        db,
                        kind="archive",
                        source_id=archive_id,
                        plate_id=plate_id,
                        file_path=file_path,
                        request_id=request_id,
                    )
                    if preview is not None:
                        used_slot_ids = {f["slot_id"] for f in preview}
                fallback_all_used = not used_slot_ids
                for f in project_filaments:
                    f["used_in_plate"] = fallback_all_used or f["slot_id"] in used_slot_ids
                filaments = project_filaments

            # Sort by slot ID
            filaments.sort(key=lambda x: x["slot_id"])

            # Enrich with nozzle mapping for dual-nozzle printers
            nozzle_mapping = extract_nozzle_mapping_from_3mf(zf)
            if nozzle_mapping:
                for filament in filaments:
                    filament["nozzle_id"] = nozzle_mapping.get(filament["slot_id"])

    except Exception as e:
        logger.warning("Failed to parse filament requirements from archive %s: %s", archive_id, e)

    return {
        "archive_id": archive_id,
        "filename": archive.filename,
        "plate_id": plate_id,
        "filaments": filaments,
    }


@router.post("/{archive_id}/reprint")
async def reprint_archive(
    archive_id: int,
    printer_id: int,
    body: ReprintRequest | None = None,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.ARCHIVES_REPRINT_ALL,
            Permission.ARCHIVES_REPRINT_OWN,
        )
    ),
):
    """Dispatch an archived 3MF file for send/start on a printer."""
    from backend.app.models.printer import Printer
    from backend.app.services.background_dispatch import DispatchEnqueueRejected, background_dispatch
    from backend.app.services.printer_manager import printer_manager

    user, can_modify_all = auth_result

    # Use defaults if no body provided
    if body is None:
        body = ReprintRequest()

    # Get archive
    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    # Ownership check
    if not can_modify_all:
        if archive.created_by_id != user.id:
            raise HTTPException(403, "You can only reprint your own archives")

    # Get printer
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    # Check printer is connected
    if not printer_manager.is_connected(printer_id):
        raise HTTPException(400, "Printer is not connected")

    if not archive.file_path:
        raise HTTPException(
            404,
            "No 3MF file available for this archive. "
            "The file could not be downloaded from the printer when the print was recorded.",
        )

    # Validate archive file exists
    file_path = settings.base_dir / archive.file_path
    if not file_path.is_file():
        raise HTTPException(404, "Archive file not found")

    plate_name = body.plate_name
    if not plate_name and body.plate_id is not None:
        plate_name = f"Plate {body.plate_id}"

    dispatch_source_name = archive.filename
    if plate_name:
        dispatch_source_name = f"{archive.filename} • {plate_name}"

    # Swap-macro execution only applies to swap-enabled printers AND files
    # that don't already carry swap macros baked in by third-party tooling
    # (``swap_compatible`` → double-fire risk). Mute the fields in either
    # case before they propagate into dispatch options or queued copies.
    if not printer.swap_mode_enabled or getattr(archive, "swap_compatible", False):
        body.execute_swap_macros = False
        body.swap_macro_events = None

    # Reprint quantity handling mirrors library print_library_file:
    # quantity == 1 → direct dispatch; quantity > 1 → all copies in queue.
    qty = max(1, body.quantity or 1)

    if qty > 1:
        from backend.app.services.queue_batch import enqueue_batch_copies

        items, batch_id = await enqueue_batch_copies(
            db,
            printer_id=printer_id,
            count=qty,
            archive_id=archive_id,
            plate_id=body.plate_id,
            ams_mapping=body.ams_mapping,
            bed_levelling=body.bed_levelling,
            flow_cali=body.flow_cali,
            layer_inspect=body.layer_inspect,
            timelapse=body.timelapse,
            use_ams=body.use_ams,
            mesh_mode_fast_check=body.mesh_mode_fast_check,
            execute_swap_macros=body.execute_swap_macros,
            swap_macro_events=body.swap_macro_events,
            created_by_id=user.id if user else None,
            project_id=archive.project_id,
        )
        logger.info(
            "Queued %s reprint copies for archive %s on printer %s (batch %s)",
            len(items),
            archive_id,
            printer_id,
            batch_id,
        )
        return {
            "status": "queued",
            "printer_id": printer_id,
            "archive_id": archive_id,
            "filename": archive.filename,
            "dispatch_job_id": None,
            "dispatch_position": None,
            "batch_id": batch_id,
            "queued_copies": len(items),
        }

    try:
        dispatch_result = await background_dispatch.dispatch_reprint_archive(
            archive_id=archive_id,
            archive_name=dispatch_source_name,
            printer_id=printer_id,
            printer_name=printer.name,
            options=body.model_dump(exclude_none=True),
            requested_by_user_id=user.id if user else None,
            requested_by_username=user.username if user else None,
        )
    except DispatchEnqueueRejected as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    logger.info(
        "Dispatched reprint archive %s for printer %s (dispatch_job_id=%s, dispatch_position=%s)",
        archive_id,
        printer_id,
        dispatch_result["dispatch_job_id"],
        dispatch_result["dispatch_position"],
    )

    batch_id: str | None = None
    extra = 0

    return {
        "status": "dispatched",
        "printer_id": printer_id,
        "archive_id": archive_id,
        "filename": archive.filename,
        "dispatch_job_id": dispatch_result["dispatch_job_id"],
        "dispatch_position": dispatch_result["dispatch_position"],
        "batch_id": batch_id,
        "queued_copies": extra,
    }


# =============================================================================
# Project Page API
# =============================================================================


@router.get("/{archive_id}/project-page")
async def get_project_page(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Get the project page data from the 3MF file."""
    from backend.app.schemas.archive import ProjectPageResponse
    from backend.app.services.archive import ProjectPageParser

    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    file_path = settings.base_dir / archive.file_path
    if not file_path.is_file():
        raise HTTPException(404, "Archive file not found")

    parser = ProjectPageParser(file_path)
    data = parser.parse(archive_id)

    return ProjectPageResponse(**data)


@router.patch("/{archive_id}/project-page")
async def update_project_page(
    archive_id: int,
    update_data: dict,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_OWN),
):
    """Update project page metadata in the 3MF file."""
    from backend.app.services.archive import ProjectPageParser

    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    file_path = settings.base_dir / archive.file_path
    if not file_path.is_file():
        raise HTTPException(404, "Archive file not found")

    parser = ProjectPageParser(file_path)
    success = parser.update_metadata(update_data)

    if not success:
        raise HTTPException(500, "Failed to update project page")

    # Return updated data
    data = parser.parse(archive_id)
    return data


@router.get("/{archive_id}/project-image/{image_path:path}")
async def get_project_image(
    archive_id: int,
    image_path: str,
    db: AsyncSession = Depends(get_db),
):
    """Get an image from the 3MF project page.

    Note: Unauthenticated - loaded via <img> tags which can't send auth headers.
    """
    from backend.app.services.archive import ProjectPageParser

    service = ArchiveService(db)
    archive = await service.get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Archive not found")

    file_path = settings.base_dir / archive.file_path
    if not file_path.is_file():
        raise HTTPException(404, "Archive file not found")

    parser = ProjectPageParser(file_path)
    result = parser.get_image(image_path)

    if not result:
        raise HTTPException(404, "Image not found in 3MF file")

    image_data, content_type = result
    return Response(
        content=image_data,
        media_type=content_type,
        headers={"Cache-Control": "max-age=3600"},
    )


# =============================================================================
# Source 3MF API (Original Project Files)
# =============================================================================


@router.post("/{archive_id}/source")
async def upload_source_3mf(
    archive_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_OWN),
):
    """Upload the original source 3MF project file for an archive."""
    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    if not file.filename or not file.filename.endswith(".3mf"):
        raise HTTPException(400, "File must be a .3mf file")

    # Get archive directory and create source subdirectory
    file_path = settings.base_dir / archive.file_path
    archive_dir = file_path.parent
    source_dir = archive_dir / "source"
    source_dir.mkdir(exist_ok=True)

    # Delete old source file if exists
    if archive.source_3mf_path:
        old_source_path = settings.base_dir / archive.source_3mf_path
        if old_source_path.exists():
            old_source_path.unlink()

    # Save the source 3MF file - preserve original filename (sanitized)
    source_filename = _safe_filename(file.filename)
    source_path = source_dir / source_filename

    content = await file.read()
    source_path.write_bytes(content)

    # Update archive with source path (relative to base_dir)
    archive.source_3mf_path = str(source_path.relative_to(settings.base_dir))

    await db.commit()
    await db.refresh(archive)

    return {
        "status": "uploaded",
        "source_3mf_path": archive.source_3mf_path,
        "filename": source_filename,
    }


@router.get("/{archive_id}/source")
async def download_source_3mf(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Download the source 3MF project file."""
    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    if not archive.source_3mf_path:
        raise HTTPException(404, "No source 3MF attached to this archive")

    source_path = settings.base_dir / archive.source_3mf_path
    if not source_path.exists():
        raise HTTPException(404, "Source 3MF file not found on disk")

    # Use the actual filename from the path
    filename = source_path.name

    return FileResponse(
        path=source_path,
        filename=filename,
        media_type="application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
    )


@router.get("/{archive_id}/source/{filename}")
async def download_source_3mf_for_slicer(
    archive_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Download source 3MF with filename in URL."""
    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    if not archive.source_3mf_path:
        raise HTTPException(404, "No source 3MF attached to this archive")

    source_path = settings.base_dir / archive.source_3mf_path
    if not source_path.exists():
        raise HTTPException(404, "Source 3MF file not found on disk")

    return FileResponse(
        path=source_path,
        filename=filename if filename.endswith(".3mf") else f"{filename}.3mf",
        media_type="application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
    )


@router.post("/{archive_id}/source-slicer-token")
async def create_source_slicer_token(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Create a short-lived download token for opening source 3MF in slicer."""
    from backend.app.core.auth import create_slicer_download_token

    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")
    if not archive.source_3mf_path:
        raise HTTPException(404, "No source 3MF attached to this archive")

    token = await create_slicer_download_token("source", archive_id)
    return {"token": token}


@router.get("/{archive_id}/source-dl/{token}/{filename}")
async def download_source_3mf_for_slicer_with_token(
    archive_id: int,
    token: str,
    filename: str,
    db: AsyncSession = Depends(get_db),
):
    """Download source 3MF using a slicer download token.

    Token-authenticated (no auth headers needed). The token is short-lived
    and single-use, created by POST /{archive_id}/source-slicer-token.
    """
    from backend.app.core.auth import verify_slicer_download_token

    if not await verify_slicer_download_token(token, "source", archive_id):
        raise HTTPException(403, "Invalid or expired download token")

    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    if not archive.source_3mf_path:
        raise HTTPException(404, "No source 3MF attached to this archive")

    source_path = settings.base_dir / archive.source_3mf_path
    if not source_path.exists():
        raise HTTPException(404, "Source 3MF file not found on disk")

    return FileResponse(
        path=source_path,
        filename=filename if filename.endswith(".3mf") else f"{filename}.3mf",
        media_type="application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
    )


@router.post("/upload-source")
async def upload_source_3mf_by_name(
    file: UploadFile = File(...),
    print_name: str = Query(None, description="Match archive by print name"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_ALL),
):
    """Upload source 3MF and match to archive by print name.

    This endpoint is designed for slicer post-processing scripts.
    It finds the most recent archive matching the print name and attaches the source.
    """
    if not file.filename or not file.filename.endswith(".3mf"):
        raise HTTPException(400, "File must be a .3mf file")

    # Derive print name from filename if not provided
    if not print_name:
        # Remove .3mf extension and common suffixes
        print_name = _safe_filename(file.filename).rsplit(".3mf", 1)[0]
        # Remove _source suffix if present
        if print_name.endswith("_source"):
            print_name = print_name[:-7]

    # Find matching archive - try exact match first, then fuzzy
    result = await db.execute(
        select(PrintArchive)
        .where(PrintArchive.print_name == print_name)
        .order_by(PrintArchive.created_at.desc())
        .limit(1)
    )
    archive = result.scalar_one_or_none()

    if not archive:
        # Try matching filename without .gcode.3mf
        result = await db.execute(
            select(PrintArchive)
            .where(PrintArchive.filename.like(f"{print_name}%"))
            .order_by(PrintArchive.created_at.desc())
            .limit(1)
        )
        archive = result.scalar_one_or_none()

    if not archive:
        # Try case-insensitive partial match on print_name
        result = await db.execute(
            select(PrintArchive)
            .where(PrintArchive.print_name.ilike(f"%{print_name}%"))
            .order_by(PrintArchive.created_at.desc())
            .limit(1)
        )
        archive = result.scalar_one_or_none()

    if not archive:
        raise HTTPException(404, f"No archive found matching '{print_name}'")

    # Get archive directory and create source subdirectory
    file_path = settings.base_dir / archive.file_path
    archive_dir = file_path.parent
    source_dir = archive_dir / "source"
    source_dir.mkdir(exist_ok=True)

    # Delete old source file if exists
    if archive.source_3mf_path:
        old_source_path = settings.base_dir / archive.source_3mf_path
        if old_source_path.exists():
            old_source_path.unlink()

    # Save the source 3MF file - preserve original filename (sanitized)
    source_filename = _safe_filename(file.filename)
    source_path = source_dir / source_filename

    content = await file.read()
    source_path.write_bytes(content)

    # Update archive with source path
    archive.source_3mf_path = str(source_path.relative_to(settings.base_dir))
    await db.commit()
    await db.refresh(archive)

    return {
        "status": "uploaded",
        "archive_id": archive.id,
        "archive_name": archive.print_name or archive.filename,
        "source_3mf_path": archive.source_3mf_path,
        "filename": source_filename,
    }


@router.delete("/{archive_id}/source")
async def delete_source_3mf(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_DELETE_OWN),
):
    """Delete the source 3MF project file from an archive."""
    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    if not archive.source_3mf_path:
        raise HTTPException(404, "No source 3MF attached to this archive")

    # Delete the file
    source_path = settings.base_dir / archive.source_3mf_path
    if source_path.exists():
        source_path.unlink()

    # Clear the path in database
    archive.source_3mf_path = None
    await db.commit()

    return {"status": "deleted"}


# =============================================================================
# F3D API (Fusion 360 Design Files)
# =============================================================================


@router.post("/{archive_id}/f3d")
async def upload_f3d(
    archive_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_OWN),
):
    """Upload a Fusion 360 design file for an archive."""
    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    if not file.filename or not file.filename.endswith(".f3d"):
        raise HTTPException(400, "File must be a .f3d file")

    # Get archive directory and create f3d subdirectory
    file_path = settings.base_dir / archive.file_path
    archive_dir = file_path.parent
    f3d_dir = archive_dir / "f3d"
    f3d_dir.mkdir(exist_ok=True)

    # Delete old F3D file if exists
    if archive.f3d_path:
        old_f3d_path = settings.base_dir / archive.f3d_path
        if old_f3d_path.exists():
            old_f3d_path.unlink()

    # Save the F3D file - preserve original filename (sanitized)
    f3d_filename = _safe_filename(file.filename)
    f3d_path = f3d_dir / f3d_filename

    content = await file.read()
    f3d_path.write_bytes(content)

    # Update archive with F3D path (relative to base_dir)
    archive.f3d_path = str(f3d_path.relative_to(settings.base_dir))

    await db.commit()
    await db.refresh(archive)

    return {
        "status": "uploaded",
        "f3d_path": archive.f3d_path,
        "filename": f3d_filename,
    }


@router.get("/{archive_id}/f3d")
async def download_f3d(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_READ),
):
    """Download the Fusion 360 design file."""
    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    if not archive.f3d_path:
        raise HTTPException(404, "No F3D file attached to this archive")

    f3d_path = settings.base_dir / archive.f3d_path
    if not f3d_path.exists():
        raise HTTPException(404, "F3D file not found on disk")

    # Use the actual filename from the path
    filename = f3d_path.name

    return FileResponse(
        path=f3d_path,
        filename=filename,
        media_type="application/octet-stream",
    )


@router.delete("/{archive_id}/f3d")
async def delete_f3d(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_DELETE_OWN),
):
    """Delete the Fusion 360 design file from an archive."""
    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive:
        raise HTTPException(404, "Archive not found")

    if not archive.f3d_path:
        raise HTTPException(404, "No F3D file attached to this archive")

    # Delete the file
    f3d_path = settings.base_dir / archive.f3d_path
    if f3d_path.exists():
        f3d_path.unlink()

    # Clear the path in database
    archive.f3d_path = None
    await db.commit()

    return {"status": "deleted"}


# =====================================================================
# Server-side slicing — re-slice an archive's source (B.4 / Phase 1.D)
# =====================================================================


@router.post("/{archive_id}/slice", status_code=202)
async def slice_archive(
    archive_id: int,
    request_body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.LIBRARY_UPLOAD),
):
    """Enqueue a slice job for an archive's source. Returns 202 + job_id;
    the slice runs in the background, the caller polls
    ``GET /slice-jobs/{id}``.

    Source preference: ``source_3mf_path`` (the un-sliced project file the
    user originally sent to slice) → ``file_path`` (the sliced 3MF/gcode
    that actually printed).
    """
    from pathlib import Path

    from backend.app.api.routes.library import slice_and_persist_as_archive
    from backend.app.core.database import async_session
    from backend.app.schemas.slicer import SliceRequest
    from backend.app.services.slice_dispatch import (
        http_exception_to_job_error,
        slice_dispatch,
    )

    try:
        request = SliceRequest.model_validate(request_body)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    archive = await db.get(PrintArchive, archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not found")

    src_relative = archive.source_3mf_path or archive.file_path
    if not src_relative:
        raise HTTPException(status_code=400, detail="Archive has no source file to slice")

    src_path = Path(settings.base_dir) / src_relative
    if not src_path.exists():
        raise HTTPException(status_code=404, detail="Archive source file missing on disk")

    raw_filename = archive.filename or src_path.name
    src_lower = raw_filename.lower()
    if not (
        src_lower.endswith(".stl")
        or src_lower.endswith(".3mf")
        or src_lower.endswith(".step")
        or src_lower.endswith(".stp")
    ):
        raise HTTPException(
            status_code=400,
            detail="Archive's source file must be STL, 3MF, or STEP to slice",
        )

    # Match the library route: derive the sliced output's filename from
    # ``print_name`` when set, so the new archive row's display name lines
    # up with the source's display.
    src_ext = Path(raw_filename).suffix.lower() or ".3mf"
    src_filename = (
        f"{archive.print_name.strip()}{src_ext}" if archive.print_name and archive.print_name.strip() else raw_filename
    )

    model_bytes = src_path.read_bytes()
    archive_id_local = archive.id
    user_id = current_user.id if current_user else None

    async def _run(job_id: int):
        async with async_session() as task_db:
            # Re-fetch the source archive on the background-task session.
            src_archive = await task_db.get(PrintArchive, archive_id_local)
            if src_archive is None:
                raise http_exception_to_job_error(
                    HTTPException(status_code=404, detail="Archive disappeared during slice")
                )
            try:
                response = await slice_and_persist_as_archive(
                    task_db,
                    model_bytes=model_bytes,
                    model_filename=src_filename,
                    request=request,
                    source_archive=src_archive,
                    current_user_id=user_id,
                    job_id=job_id,
                )
            except HTTPException as exc:
                raise http_exception_to_job_error(exc) from exc
        return response.model_dump()

    job = await slice_dispatch.enqueue(
        kind="archive",
        source_id=archive.id,
        source_name=archive.print_name or archive.filename or f"archive {archive.id}",
        run=_run,
    )
    return {
        "job_id": job.id,
        "status": job.status,
        "status_url": f"/api/v1/slice-jobs/{job.id}",
    }
