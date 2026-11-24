"""Read-only Prometheus projections of the existing workshop and spool ledgers.

These are mutable accounting snapshots, not event counters or physical scales.
No collector reserves stock, settles usage or repairs production state.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SERVICE_USAGE_STATUSES, SpoolUsageHistory
from backend.app.services import auto_stock_spool, inventory_service, order_fulfilment, order_metrics
from backend.app.services.order_queue import awaiting_auto_row_conditions, live_archive_conditions

# Bound hydration and series churn. Never present a truncated order as complete.
MAX_ORDERS = 50
MAX_LINES = 1000
MAX_ARCHIVES = 10000
MAX_PART_ROWS = 100000


async def stock_metrics(db, emit):
    stats = await inventory_service.inventory_stats(db)
    emit(
        "inventory_spools",
        "Inventory spool records; active excludes archived records",
        stats["active_spools"],
        scope="active",
    )
    emit(
        "inventory_spools",
        "Inventory spool records; active excludes archived records",
        stats["total_spools"],
        scope="all",
    )
    emit(
        "inventory_remaining_grams",
        "Estimated remaining grams on active spools, clamped per spool as in Inventory",
        stats["total_weight_g"],
    )
    emit(
        "inventory_low_stock_spools",
        "Active spools below their configured remaining-weight threshold",
        stats["low_stock_count"],
    )
    free_count, free_grams = (
        await db.execute(
            select(func.count(), func.coalesce(func.sum(Spool.label_weight), 0)).where(
                *auto_stock_spool.available_filters()
            )
        )
    ).one()
    emit("stock_full_spools", "Unused unassigned spools eligible for the existing automatic stock selector", free_count)
    emit(
        "stock_full_grams", "Label grams of unused unassigned spools eligible for automatic stock selection", free_grams
    )
    overdrawn = await db.scalar(
        select(func.count())
        .select_from(Spool)
        .where(Spool.archived_at.is_(None), Spool.weight_used > Spool.label_weight)
    )
    emit("inventory_overdrawn_spools", "Active spools whose accounted consumption exceeds label weight", overdrawn)
    # Per physical slot, not per historical spool; bounded by configured slots.
    for assignment, spool in (await db.execute(select(SpoolAssignment, Spool).join(Spool))).all():
        labels = {
            "printer_id": str(assignment.printer_id),
            "ams_id": str(assignment.ams_id),
            "tray_id": str(assignment.tray_id),
        }
        emit(
            "assigned_spool_balance_grams",
            "Signed label weight minus lifetime usage for the assigned spool; estimate, not a scale",
            spool.label_weight - spool.weight_used,
            **labels,
        )
        emit(
            "assigned_spool_archived",
            "Assigned spool is archived; inspect inventory before relying on its balance",
            int(spool.archived_at is not None),
            **labels,
        )


async def usage_metrics(db, emit, now):
    # Settlement timestamp, not print start time. Failed attempts still consumed
    # filament. Known service corrections remain a separate signed series.
    windows = {"24h": 1, "7d": 7, "30d": 30}
    conditions = {label: SpoolUsageHistory.created_at >= now - timedelta(days=days) for label, days in windows.items()}
    kind = case((SpoolUsageHistory.status.in_(SERVICE_USAGE_STATUSES), "service_adjustment"), else_="print")
    rows = await db.execute(
        select(
            SpoolUsageHistory.printer_id,
            kind.label("kind"),
            *(
                func.sum(case((condition, SpoolUsageHistory.weight_used), else_=0)).label(label)
                for label, condition in conditions.items()
            ),
        )
        .where(SpoolUsageHistory.created_at >= now - timedelta(days=30), SpoolUsageHistory.created_at <= now)
        .group_by(SpoolUsageHistory.printer_id, kind)
    )
    # Empty windows are explicitly zero, also after the last usage ages out.
    printer_ids = list((await db.scalars(select(Printer.id))).all()) + [None]
    totals = {(row[0], row[1]): row[2:] for row in rows}
    for printer_id in printer_ids:
        for category in ("print", "service_adjustment"):
            values = totals.get((printer_id, category), (0, 0, 0))
            for window, value in zip(windows, values, strict=True):
                emit(
                    "accounted_filament_window_grams",
                    "Signed booked spool-ledger grams by settlement time in a rolling window; gauge, do not rate or increase",
                    value,
                    printer_id=str(printer_id) if printer_id is not None else "unassigned",
                    kind=category,
                    window=window,
                )


async def queue_metrics(db, emit, manager, now):
    auto_pending = await db.scalar(
        select(func.count()).select_from(AutoQueueItem).where(*awaiting_auto_row_conditions())
    )
    printer_pending = await db.scalar(
        select(func.count()).select_from(PrintQueueItem).where(PrintQueueItem.status == "pending")
    )
    for tier, value in (("auto", auto_pending), ("printer", printer_pending)):
        emit(
            "pending_jobs",
            "Waiting jobs by queue tier; assigned AutoQueue rows are not counted twice",
            value,
            tier=tier,
        )
    queues = {q.printer_id: q for q in (await db.scalars(select(PrinterQueue))).all()}
    for printer in (await db.scalars(select(Printer).where(Printer.archived.is_(False)))).all():
        labels = {"printer_id": str(printer.id)}
        emit(
            "printer_maintenance",
            "Printer is in Maintenance Mode; independent of queue pause",
            int(not printer.is_active),
            **labels,
        )
        emit(
            "printer_awaiting_plate_clear",
            "BamDude plate-clear gate is armed; not physical proof of plate contents",
            int(printer.awaiting_plate_clear),
            **labels,
        )
        queue = queues.get(printer.id)
        if queue is not None:
            emit(
                "printer_queue_paused",
                "Operator pause of the per-printer queue, independent of the running print",
                int(queue.is_paused),
                **labels,
            )
            emit(
                "printer_auto_distribute_eligible",
                "Printer queue allows AutoQueue distribution; other gates may still block it",
                int(queue.auto_distribute_eligible),
                **labels,
            )
        client = manager.get_client(printer.id)
        received = client.status_received_at if client is not None else None
        emit(
            "printer_telemetry_timestamp_known",
            "Whether a last local MQTT reception timestamp is available",
            int(received is not None),
            **labels,
        )
        if received is not None:
            emit(
                "printer_telemetry_age_seconds",
                "Age of last locally received MQTT message, not freshness of every individual field",
                max(0, now.replace(tzinfo=timezone.utc).timestamp() - received),
                **labels,
            )
    # Real archive foreign keys only. A queue claim/state is not proof of a
    # current physical print, and several links are kept separate, not guessed.
    rows = await db.execute(
        select(PrintArchive.printer_id, PrintArchive.project_id, PrintArchive.project_line_id, func.count())
        .where(PrintArchive.status == "printing", *live_archive_conditions())
        .group_by(PrintArchive.printer_id, PrintArchive.project_id, PrintArchive.project_line_id)
    )
    for printer_id, order_id, line_id, count in rows:
        emit(
            "running_order_archives",
            "Archives recorded as printing, linked by stored foreign keys; unknown links are explicit",
            count,
            printer_id=str(printer_id) if printer_id is not None else "unknown",
            order_id=str(order_id) if order_id is not None else "unknown",
            line_id=str(line_id) if line_id is not None else "unknown",
        )


async def order_metrics_snapshot(db, emit):
    order_ids = list(
        (
            await db.scalars(
                select(Project.id).where(Project.status == "active").order_by(Project.id).limit(MAX_ORDERS + 1)
            )
        ).all()
    )
    active_count = await db.scalar(select(func.count()).select_from(Project).where(Project.status == "active"))
    emit("active_orders", "Active order records, including any omitted by the bounded detail reader", active_count)
    counts = [len(order_ids)]
    for statement in (
        select(func.count()).select_from(ProjectLine).where(ProjectLine.project_id.in_(order_ids)),
        select(func.count())
        .select_from(PrintArchive)
        .where(PrintArchive.project_id.in_(order_ids), *live_archive_conditions()),
        select(func.count())
        .select_from(PrintArchivePart)
        .join(PrintArchive)
        .where(PrintArchive.project_id.in_(order_ids), *live_archive_conditions()),
    ):
        counts.append(await db.scalar(statement))
    bounded = all(
        n <= limit for n, limit in zip(counts, (MAX_ORDERS, MAX_LINES, MAX_ARCHIVES, MAX_PART_ROWS), strict=True)
    )
    emit(
        "order_details_available",
        "All active orders fit the documented hydration limits; 0 means detail metrics are omitted, not zero production",
        int(bounded),
    )
    if not bounded:
        return
    for ctx in await order_metrics.batch_contexts(db, order_ids):
        awards, uncertain = [], {}
        figures, other = order_metrics.attribute(ctx, part_attributions=awards, uncertain_parts=uncertain)
        fulfilment = await order_fulfilment.state(db, ctx.project, ctx=ctx, figures=figures)
        receipts = {row.line_id: row for row in fulfilment.lines}
        archives = {a.id: a for a in ctx.archives}
        quality = defaultdict(lambda: [0, 0])
        for award in awards:
            index = 0 if archives[award.archive_id].outcome_checked_at is not None else 1
            quality[award.line_id, award.part_id][index] += award.quantity
        unknown = set().union(*uncertain.values()) if uncertain else set()
        missing_rows = any(a.status == "completed" and not ctx.archive_parts_by_archive.get(a.id) for a in ctx.archives)
        labels = {"order_id": str(ctx.project.id)}
        emit(
            "order_quality_coverage_complete",
            "All completed archives have part rows; incomplete history is not evidence of zero defects",
            int(not missing_rows),
            **labels,
        )
        emit(
            "order_recorded_defective_pieces",
            "Recorded defective part rows across this order, including failed attempts; NaN if completed part rows are missing",
            float("nan")
            if missing_rows
            else sum(p.defective for rows in ctx.archive_parts_by_archive.values() for p in rows),
            **labels,
        )
        emit(
            "order_unattributed_archives",
            "Order archives not attributable to a counted line by the canonical reader",
            len(other),
            **labels,
        )
        emit(
            "order_unchecked_archives",
            "Completed order archives without an outcome check; not accepted good output",
            sum(a.status == "completed" and a.outcome_checked_at is None for a in ctx.archives),
            **labels,
        )
        emit(
            "order_can_complete",
            "Order satisfies the existing Workshop completion rules",
            int(fulfilment.can_complete),
            **labels,
        )
        for line in ctx.lines:
            f, receipt = figures[line.id], receipts[line.id]
            line_labels = dict(**labels, line_id=str(line.id), unit="product_units" if f.mode == "product" else "parts")
            for field, value in {
                "ordered": f.quantity,
                "ui_printed": f.units_printed,
                "ui_covered": f.covered_units,
                "ui_remaining": max(0, f.quantity - f.covered_units),
                "from_finished_stock": f.from_finished,
                "from_part_kits": f.from_kit_units,
                "held": receipt.held,
                "issued": receipt.issued,
            }.items():
                emit(
                    "order_line_units",
                    "Canonical Workshop line quantities; ui counts include unchecked outcomes; stock/receipts are not extra production",
                    value,
                    **line_labels,
                    quantity=field,
                )
            for stage, value in (("running", f.prints_in_progress), ("queued", f.prints_queued)):
                emit(
                    "order_line_jobs",
                    "Canonical attributed running archives or waiting jobs across both queue tiers",
                    value,
                    **labels,
                    line_id=str(line.id),
                    stage=stage,
                )
            for part in f.parts:
                part_labels = dict(**labels, line_id=str(line.id), part_id=str(part.part_id))
                known = (line.id, part.part_id) not in unknown
                checked, unchecked = quality[line.id, part.part_id] if known else (float("nan"), float("nan"))
                emit(
                    "order_part_quality_known",
                    "Checked/unchecked attribution is known; 0 is accompanied by NaN quantities, not fabricated zeros",
                    int(known),
                    **part_labels,
                )
                for field, value in {
                    "ui_usable": part.usable,
                    "ui_remaining": part.remaining,
                    "recount_adjustment": part.count_adjustment,
                    "allocated_stock": part.allocated_qty,
                    "checked_net": checked,
                    "unchecked_net": unchecked,
                }.items():
                    emit(
                        "order_part_pieces",
                        "Canonical part quantities; checked/unchecked are attributed net archive output before recounts, not stock receipts",
                        value,
                        **part_labels,
                        quantity=field,
                    )


async def collect_production_metrics(db, emit, manager, *, now=None):
    """Use the caller's read session and existing readers; never flush or commit."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    with db.no_autoflush:
        await stock_metrics(db, emit)
        await usage_metrics(db, emit, now)
        await queue_metrics(db, emit, manager, now)
        await order_metrics_snapshot(db, emit)
    emit(
        "production_snapshot_timestamp_seconds",
        "UTC collection time for production accounting snapshots; individual telemetry has its own age",
        now.replace(tzinfo=timezone.utc).timestamp(),
    )
