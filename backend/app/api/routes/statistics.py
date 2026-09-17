"""Statistics: everything the Stats page reads, in one router.

Moved out of ``routes/archives.py`` on 2026-09-17 (vault
60-specs/statistics-module-spec). Bodies are verbatim; only the paths changed -
``/archives/stats`` -> ``/statistics/overview``, ``/archives/analysis/failures``
-> ``/statistics/failures``, ``/archives/stats/export`` -> ``/statistics/export``;
``aggregate`` and ``recalculate-costs`` keep their last segment. The old paths
are gone, not aliased (ruling 2026-09-17: our own frontend moves, anything
else rewrites).

Permissions travel unchanged and deliberately differ: ``overview`` and
``export`` under ``stats:read``, ``aggregate`` and ``failures`` under
``archives:read_all`` / ``read_own`` with the same narrowing to one's own
prints, ``recalculate-costs`` under ``archives:update_all``. Unifying them is an
access decision, not part of the move.
"""

import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission, require_ownership_permission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.timezones import client_timezone, day_bounds
from backend.app.models.archive import PrintArchive
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.models.user import User
from backend.app.schemas.statistics import ArchiveAggregate, ArchiveStats, DefectsByPrinter
from backend.app.services.filament_cost import default_rate_per_kg
from backend.app.services.statistics import aggregate as archive_aggregate
from backend.app.services.statistics.energy import sum_live_plug_totals, sum_snapshot_deltas
from backend.app.utils.http import build_content_disposition

router = APIRouter(prefix="/statistics", tags=["statistics"])

# Reserved: the reports subsystem (vault TaskNote «Винести статистику з archives
# у власний модуль», follow-up). No endpoints yet - the path has an owner.
reports_router = APIRouter(prefix="/reports", tags=["statistics"])
router.include_router(reports_router)


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


@router.get("/overview", response_model=ArchiveStats)
async def get_archive_stats(
    request: Request,
    date_from: date | None = Query(None, description="Start date (inclusive), YYYY-MM-DD"),
    date_to: date | None = Query(None, description="End date (inclusive), YYYY-MM-DD"),
    created_by_id: int | None = Query(None, description="Filter by user who created the print (-1 for no user)"),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.STATS_READ),
):
    """Get statistics across all archives."""
    _validate_user_filter_permission(current_user, created_by_id)

    # Build date filter conditions.
    # Defensively exclude the legacy "archived" status (uploaded-but-never-printed
    # rows from the removed VP / manual-upload flows; no longer produced, but
    # legacy DBs may still carry them).
    # Exclude trashed rows (deleted_at IS NOT NULL) — trash is a soft-delete
    # awaiting the retention sweeper, the user has explicitly removed these
    # from active history and they shouldn't pollute totals / filament / cost.
    base_conditions = [
        PrintArchive.status != "archived",
        PrintArchive.deleted_at.is_(None),
    ]
    _apply_user_filter(base_conditions, created_by_id)
    # Client's day, not UTC's — the picker was filled in against their clock.
    # This decides which prints land in the range at all, so a UTC boundary
    # moved every print from the first hours of a local day into the one before.
    _tz = client_timezone(request)
    if date_from:
        base_conditions.append(PrintArchive.created_at >= day_bounds(date_from, _tz)[0])
    if date_to:
        base_conditions.append(PrintArchive.created_at < day_bounds(date_to, _tz)[1])

    # Total counts
    total_result = await db.execute(select(func.count(PrintArchive.id)).where(*base_conditions))
    total_prints = total_result.scalar() or 0

    successful_result = await db.execute(
        select(func.count(PrintArchive.id)).where(PrintArchive.status == "completed", *base_conditions)
    )
    successful_prints = successful_result.scalar() or 0

    failed_result = await db.execute(
        select(func.count(PrintArchive.id)).where(PrintArchive.status.in_(["failed", "aborted"]), *base_conditions)
    )
    failed_prints = failed_result.scalar() or 0

    # User/system-stopped prints — stopped/cancelled/skipped are distinct from
    # quality failures: the user (or the queue) interrupted them, the printer
    # didn't detect a fault. Bucketed separately so the Success Rate gauge
    # divides by completed + failed only (a cancelled print shouldn't drag the
    # gauge down), while still being visible in the breakdown so they don't
    # silently vanish from Total Prints (#1390).
    cancelled_result = await db.execute(
        select(func.count(PrintArchive.id)).where(
            PrintArchive.status.in_(["stopped", "cancelled", "skipped"]), *base_conditions
        )
    )
    cancelled_prints = cancelled_result.scalar() or 0

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

    # Defects by printer — completed prints only: what came off the plate
    # against what the operator (or a skip) marked bad (spec 2026-09-11 §6).
    defects_result = await db.execute(
        select(
            PrintArchive.printer_id,
            func.coalesce(func.sum(PrintArchive.quantity), 0),
            func.coalesce(func.sum(PrintArchive.defective_count), 0),
        )
        .where(PrintArchive.status == "completed", *base_conditions)
        .group_by(PrintArchive.printer_id)
    )
    defects_by_printer = {
        str(printer_id): DefectsByPrinter(printed=int(printed), defective=int(defective))
        for printer_id, printed, defective in defects_result.all()
        if printer_id is not None and int(printed) > 0
    }

    # Time accuracy statistics
    # Completed prints that carry both an estimate and a measured time.
    #
    # ⚠️ THREE COLUMNS, never `select(PrintArchive)`. This used to hydrate whole
    # ORM entities — every column, the JSON blob, the identity map — for tens of
    # thousands of rows, to read two flags and a float off each. That hydration
    # runs on the event loop, so opening the stats page stalled every printer's
    # MQTT for as long as it took. `time_accuracy IS NOT NULL` moved into the
    # WHERE for the same reason: those rows were fetched and then skipped.
    accuracy_rows = (
        await db.execute(
            select(PrintArchive.printer_id, PrintArchive.time_accuracy, PrintArchive.extra_data)
            .where(PrintArchive.status == "completed", *base_conditions)
            .where(PrintArchive.print_time_seconds.isnot(None))
            .where(PrintArchive.started_at.isnot(None))
            .where(PrintArchive.completed_at.isnot(None))
            .where(PrintArchive.time_accuracy.isnot(None))
        )
    ).all()

    average_accuracy = None
    accuracy_by_printer: dict[str, float] = {}

    if accuracy_rows:
        accuracies = []
        printer_accuracies: dict[str, list[float]] = {}

        for printer_id, time_accuracy, extra_data in accuracy_rows:
            # Skip synthetic closures. Their completed_at is derived FROM the
            # slicer estimate (reconcile / stale-cleanup), so time_accuracy is
            # 100% by construction and would drag the fleet average toward a
            # number nobody measured (#2592).
            extra = extra_data or {}
            if extra.get("recovered_by_startup_sweep") or extra.get("recovered_by_cleanup"):
                continue
            accuracies.append(time_accuracy)

            # Group by printer
            printer_key = str(printer_id) if printer_id else "unknown"
            if printer_key not in printer_accuracies:
                printer_accuracies[printer_key] = []
            printer_accuracies[printer_key].append(time_accuracy)

        if accuracies:
            average_accuracy = round(sum(accuracies) / len(accuracies), 1)

        # Calculate per-printer averages
        for printer_key, accs in printer_accuracies.items():
            accuracy_by_printer[printer_key] = round(sum(accs) / len(accs), 1)

    # Energy, both ways — see ArchiveStats for why there are two of each.
    from backend.app.api.routes.settings import get_setting

    energy_cost_per_kwh_str = await get_setting(db, "energy_cost_per_kwh")
    energy_cost_per_kwh = float(energy_cost_per_kwh_str) if energy_cost_per_kwh_str else 0.15

    total_energy_kwh: float = 0.0
    total_energy_cost: float = 0.0
    energy_data_warming_up = False

    # ── What the prints themselves drew ──────────────────────────────────
    # The per-print column, summed. Recorded from the plug at the start and end
    # of each print, so it excludes everything between prints.
    print_energy_kwh = (
        await db.execute(select(func.sum(PrintArchive.energy_kwh)).where(*base_conditions))
    ).scalar() or 0
    print_energy_cost = (
        await db.execute(select(func.sum(PrintArchive.energy_cost)).where(*base_conditions))
    ).scalar() or 0

    # ── What the plugs measured, full stop ───────────────────────────────
    if not date_from and not date_to:
        # All-time: the live lifetime counters.
        total_energy_kwh = await sum_live_plug_totals(db)
        total_energy_cost = total_energy_kwh * energy_cost_per_kwh
    else:
        # Total consumption mode with a date filter (#941): use hourly snapshots
        # to compute per-plug (endpoint - baseline) deltas.
        #
        # Day boundaries are the CLIENT's, not UTC. The dates arrive as bare
        # calendar days from a date picker someone filled in while looking at
        # their own clock, so resolving them at UTC midnight put the first hours
        # of every local day into the previous one — three hours' worth on a
        # Europe/Kyiv farm, which is where this was noticed.
        tz = client_timezone(request)
        dt_from = day_bounds(date_from, tz)[0] if date_from else None
        # Exclusive end from day_bounds: `time.max` would silently drop the last
        # 999 microseconds of the range.
        dt_to = day_bounds(date_to, tz)[1] if date_to else None

        total_energy_kwh, energy_data_warming_up = await sum_snapshot_deltas(db, dt_from=dt_from, dt_to=dt_to)
        total_energy_cost = total_energy_kwh * energy_cost_per_kwh

    return ArchiveStats(
        total_prints=total_prints,
        successful_prints=successful_prints,
        failed_prints=failed_prints,
        cancelled_prints=cancelled_prints,
        total_print_time_hours=round(total_time, 1),
        total_filament_grams=round(total_filament, 1),
        total_cost=round(total_cost, 2),
        prints_by_filament_type=prints_by_filament,
        prints_by_printer=prints_by_printer,
        defects_by_printer=defects_by_printer,
        average_time_accuracy=average_accuracy,
        time_accuracy_by_printer=accuracy_by_printer if accuracy_by_printer else None,
        print_energy_kwh=round(print_energy_kwh, 3),
        print_energy_cost=round(print_energy_cost, 3),
        total_energy_kwh=round(total_energy_kwh, 3),
        total_energy_cost=round(total_energy_cost, 3),
        energy_data_warming_up=energy_data_warming_up,
    )


@router.get("/aggregate", response_model=ArchiveAggregate)
async def aggregate_archives(
    request: Request,
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.ARCHIVES_READ_ALL,
            Permission.ARCHIVES_READ_OWN,
        )
    ),
):
    """Everything the Stats page and the archive calendar fold, folded here.

    The response is sized by the date range, not by the number of prints — and,
    unlike the slim listing it replaces, it is not silently truncated at the
    newest 10 000 rows, which on a busy farm turned «all time» into «the last
    three weeks» without saying so.

    Day and hour keys are local to the caller's ``X-Client-Timezone`` (the
    server's ``TZ`` when the header is absent), which is where the browser was
    bucketing them before — so the numbers are the same, computed once instead
    of per viewer.
    """
    current_user, can_read_all = auth_result
    return await archive_aggregate.collect(
        db,
        tz=client_timezone(request),
        date_from=date_from,
        date_to=date_to,
        user_id=None if can_read_all or current_user is None else current_user.id,
    )


@router.get("/failures")
async def analyze_failures(
    request: Request,
    days: int | None = None,
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    printer_id: int | None = None,
    project_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.ARCHIVES_READ_ALL,
            Permission.ARCHIVES_READ_OWN,
        )
    ),
):
    """Analyze failure patterns across prints.

    Returns failure statistics including:
    - Overall failure rate
    - Failures by reason, filament type, printer
    - Time of day distribution
    - Recent failures
    - Weekly trend
    """
    # security #2: gated by require_ownership_permission above; READ_OWN callers
    # are scoped to their own runs so aggregate failure stats don't leak other
    # users' prints.
    user, can_read_all = auth_result
    scoped_user_id = user.id if (user is not None and not can_read_all) else None

    from backend.app.services.statistics.failure_analysis import FailureAnalysisService

    service = FailureAnalysisService(db)
    return await service.analyze_failures(
        days=days,
        date_from=date_from,
        date_to=date_to,
        printer_id=printer_id,
        project_id=project_id,
        created_by_id=scoped_user_id,
        tz=client_timezone(request),
    )


@router.get("/export")
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
        headers={"Content-Disposition": build_content_disposition(filename)},
    )


@router.post("/recalculate-costs")
async def recalculate_all_costs(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.ARCHIVES_UPDATE_ALL),
):
    """Recalculate costs for all archives based on filament usage and prices."""

    result = await db.execute(select(PrintArchive))
    archives = list(result.scalars().all())

    # Get default filament cost from settings
    default_cost_per_kg = await default_rate_per_kg(db)

    # Pre-fetch all usage costs and tracked weight by archive_id. Tracked
    # weight tops up the cost at the default rate for any filament grams not
    # covered by an inventory spool (#1344).
    usage_costs_result = await db.execute(
        select(
            SpoolUsageHistory.archive_id,
            func.sum(SpoolUsageHistory.cost),
            func.sum(SpoolUsageHistory.weight_used),
        ).group_by(SpoolUsageHistory.archive_id)
    )
    usage_costs = usage_costs_result.fetchall()
    cost_map = {
        row[0]: (row[1], float(row[2] or 0))
        for row in usage_costs
        if row[0] is not None and row[1] is not None and row[1] > 0
    }

    updated = 0
    for archive in archives:
        usage = cost_map.get(archive.id)
        if usage is not None:
            usage_cost, tracked_grams = usage
            total_cost = float(usage_cost)
            archive_grams = float(archive.filament_used_grams or 0)
            untracked_grams = max(0.0, archive_grams - tracked_grams)
            if untracked_grams > 0 and default_cost_per_kg > 0:
                total_cost += (untracked_grams / 1000.0) * default_cost_per_kg
            new_cost = round(total_cost, 2)
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
            elif archive.filament_used_grams and default_cost_per_kg > 0:
                new_cost = round((archive.filament_used_grams / 1000) * default_cost_per_kg, 2)
            else:
                new_cost = None
        if new_cost is not None and archive.cost != new_cost:
            archive.cost = new_cost
            updated += 1

    await db.commit()
    return {"message": f"Recalculated costs for {updated} archives", "updated": updated}
