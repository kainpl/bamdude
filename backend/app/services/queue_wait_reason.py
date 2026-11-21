"""Record scheduling observations without changing dispatch authority."""

from datetime import datetime, timezone
from typing import Literal

WaitCode = Literal[
    "printer_offline",
    "plate_not_cleared",
    "drying",
    "scheduled",
    "manual_start",
    "stagger",
    "filament_unavailable",
    "dispatch_runtime_conflict",
    "dispatch_failed",
    "storage_full",
    # The source copy found the spool busy; the scheduler asks again after its bounded cooldown.
    "source_capture",
    "unknown",
    "order_resources",
]


def set_wait_reason(item, code: WaitCode | None, message: str | None) -> bool:
    """Return whether the row changed; repeated observations do not cause writes.

    checked_at identifies when this particular decision was established. The
    monitor still checks live facts before using it, and edits invalidate it.
    """
    if item.waiting_reason == message and getattr(item, "waiting_reason_code", None) == code:
        return False
    item.waiting_reason = message
    item.waiting_reason_code = code
    item.waiting_reason_checked_at = datetime.now(timezone.utc) if code else None
    return True


async def write_wait_reason(db, item, code: WaitCode | None, message: str | None, *, queue_id: int) -> bool:
    """The scheduler's waiting diagnostic on a printer row, written addressed (G03 B, A8, L2(b)).

    One Core UPDATE by id, ``status = 'pending'`` and the queue the pass loaded the row in — never a flush of the
    pass's copy, which would carry whatever else that copy holds. A repeated observation writes nothing. On a
    write the copy follows by ``set_committed_value``. False: the row is no longer the pending row of that queue
    the pass loaded, and the pass writes nothing else for it.
    """
    from sqlalchemy import update
    from sqlalchemy.orm.attributes import set_committed_value

    from backend.app.models.print_queue import PrintQueueItem

    if item.waiting_reason == message and getattr(item, "waiting_reason_code", None) == code:
        return True
    checked_at = datetime.now(timezone.utc) if code else None
    result = await db.execute(
        update(PrintQueueItem)
        .where(PrintQueueItem.id == item.id, PrintQueueItem.status == "pending", PrintQueueItem.queue_id == queue_id)
        .values(waiting_reason=message, waiting_reason_code=code, waiting_reason_checked_at=checked_at)
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        return False
    set_committed_value(item, "waiting_reason", message)
    set_committed_value(item, "waiting_reason_code", code)
    set_committed_value(item, "waiting_reason_checked_at", checked_at)
    return True
