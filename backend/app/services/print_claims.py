"""Address one observed execution under the existing archive/printer lane guards."""

from dataclasses import dataclass

from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.printer_queues import ensure_printer_queue
from backend.app.services.queue_batch import claim_printer_for_direct_print
from backend.app.services.queue_counters import set_queue_printing


@dataclass(frozen=True)
class ClaimResult:
    state: str
    item_id: int | None = None
    item_identity: str | None = None
    archive_identity: str | None = None

    def __bool__(self):
        return self.state in {"owned", "created"}


async def observe_claim(db, printer_id, item_id=None, *, archive_id=None, options=None, expected_identity=None):
    """Caller holds claim_lanes and a clean writer transaction; never commits."""
    queue = await ensure_printer_queue(db, printer_id)
    archive = await db.get(PrintArchive, archive_id, populate_existing=True) if archive_id else None
    if archive_id and (
        archive is None
        or (expected_identity is not None and archive.recovery_identity != expected_identity)
        or archive.printer_id != printer_id
        or archive.status != "printing"
        or archive.deleted_at is not None
        or "noncurrent_execution" in (archive.extra_data or {})
    ):
        return ClaimResult("unresolved")
    rows = list(
        (
            await db.scalars(
                select(PrintQueueItem)
                .where(PrintQueueItem.queue_id == queue.id, PrintQueueItem.status == "printing")
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        ).all()
    )
    matching = [r for r in rows if archive_id is not None and r.archive_id == archive_id]
    selected = None
    if len(matching) > 1:
        queue.is_paused = True
        return ClaimResult("unresolved")
    if item_id is not None:
        selected = next((r for r in rows if r.id == item_id), None)
        if selected is None or selected.archive_id not in (None, archive_id):
            queue.is_paused = True
            return ClaimResult("unresolved")
    elif matching:
        selected = matching[0]
    elif len(rows) == 1 and rows[0].archive_id is None and (rows[0].origin == "direct" or archive_id is None):
        selected = rows[0]
    if selected is not None:
        if archive_id is not None and selected.archive_id is None:
            selected.archive_id = archive_id
        state = "owned"
    else:
        # No guessing from latest/filename. A separately proven external B has
        # its own archive; unrelated old claims pause the lane, not erase B.
        if rows and archive_id is None:
            queue.is_paused = True
            return ClaimResult("unresolved")
        selected = await claim_printer_for_direct_print(
            db,
            printer_id=printer_id,
            origin="external",
            archive_id=archive_id,
            options=options,
            commit=False,
            lane_held=True,
        )
        state = "created"
        if rows:
            queue.is_paused = True
    await set_queue_printing(db, queue.id, selected.id)
    if archive is not None:
        from backend.app.services.dispatch_phase import mark_seen_running

        mark_seen_running(archive)
    return ClaimResult(state, selected.id, selected.recovery_identity, archive.recovery_identity if archive else None)
