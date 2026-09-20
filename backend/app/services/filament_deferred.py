"""Bookkeeping for an attempt refused before MQTT publish; never a failed print."""

import json
from datetime import datetime, timezone

from sqlalchemy import func, update

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.filament_intake import routing_detail
from backend.app.services.filament_policy import decode
from backend.app.services.queue_counters import update_queue_counters
from backend.app.services.source_io import SOURCE_FAILURES


def dispatched_archive_filter():
    return func.coalesce(PrintArchive.extra_data["dispatch_aborted"].as_boolean(), False).is_(False)


async def abort_execution_archive(db, archive_id, reason):
    if archive_id is None:
        return
    archive = await db.get(PrintArchive, archive_id)
    if archive is not None and archive.status == "printing":
        archive.status = "cancelled"
        archive.extra_data = {**(archive.extra_data or {}), "dispatch_aborted": True, "dispatch_abort_reason": reason}
        archive.completed_at = datetime.now(timezone.utc)


async def defer_claim(
    db,
    *,
    item_id,
    started_at,
    reason,
    revision=None,
    direct=False,
    source_archive_id=None,
    source_library_file_id=None,
    restore_source=False,
):
    """CAS only our active attempt. Cancelled/deleted/reclaimed rows stay untouched."""
    item = await db.get(PrintQueueItem, item_id)
    if item is None or started_at is None:
        return False
    source_failed = not direct and reason in SOURCE_FAILURES
    values = {
        "status": "cancelled" if direct else "failed" if source_failed else "pending",
        "started_at": None,
        "completed_at": datetime.now(timezone.utc) if direct or source_failed else None,
        "waiting_reason": None if source_failed else routing_detail(reason)["message"],
        "waiting_reason_code": None if direct or source_failed else "filament_unavailable",
        "waiting_reason_checked_at": None if direct or source_failed else datetime.now(timezone.utc),
        "error_message": routing_detail(reason)["message"] if source_failed else None,
    }
    if restore_source:
        values.update(archive_id=source_archive_id, library_file_id=source_library_file_id)
    if source_failed:
        # This preparation never published a print, so there is no physical
        # failure for the next item's require_previous_success gate.
        values["gate_acknowledged"] = True
    routing = decode(item.filament_routing)
    if isinstance(routing, dict):
        routing["runtime"] = {"reason": reason, "blocked_revision": revision}
        values["filament_routing"] = json.dumps(routing)
    result = await db.execute(
        update(PrintQueueItem)
        .where(
            PrintQueueItem.id == item_id,
            PrintQueueItem.status == "printing",
            PrintQueueItem.started_at == started_at,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        return False
    # The queue can already belong to a later attempt; do not release that owner.
    await db.execute(
        update(PrinterQueue)
        .where(
            PrinterQueue.id == item.queue_id,
            PrinterQueue.current_item_id == item_id,
            PrinterQueue.status == "printing",
        )
        .values(status="idle", current_item_id=None)
        .execution_options(synchronize_session=False)
    )
    await update_queue_counters(db, item.queue_id)
    return True
