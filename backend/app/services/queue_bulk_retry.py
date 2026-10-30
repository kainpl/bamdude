"""Re-arm a confirmed failed/cancelled block under the scheduler's final lane guard."""

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from backend.app.core.api_key_scope import narrowed_scope
from backend.app.core.auth import authorize_api_key
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import STATE_READY, QueueSource
from backend.app.models.user import User
from backend.app.services import queue_sources
from backend.app.services.filament_policy import restore_routing_source
from backend.app.services.queue_ops import claim_lanes
from backend.app.services.swap_source_declarations import fresh_api_key


async def retry_block(db, queue_id, item_ids, *, actor, granted_scope, key_hash, cancelled_item_ids=()):
    from backend.app.services.queue_counters import update_queue_counters

    ids = list(dict.fromkeys(item_ids))
    cancelled = set(cancelled_item_ids)
    # Preflight/identity reads must end before the process lane and SQLite writer.
    await db.rollback()
    try:
        async with queue_sources.storage_mutation(), claim_lanes(db, [queue_id]):
            if actor.kind == "api_key":
                key = await fresh_api_key(db, actor)
                if key.key_hash != key_hash or key.user_id != actor.user_id:
                    raise HTTPException(403, "Retry authority changed")
                await authorize_api_key(db, key, ["queue:update_all"])
                scope = narrowed_scope(granted_scope, key)
                if scope is not None and queue_id not in scope:
                    raise HTTPException(403, "Retry printer access changed")
            else:
                user = await db.scalar(
                    select(User)
                    .options(selectinload(User.groups))
                    .where(User.id == actor.user_id)
                    .execution_options(populate_existing=True)
                )
                if user is None or not user.is_active or not user.has_permission("queue:update_all"):
                    raise HTTPException(403, "Retry authority changed")
            if await db.get(PrinterQueue, queue_id) is None:
                raise HTTPException(404, "Queue not found")
            rows = {
                row.id: row
                for row in await db.scalars(
                    select(PrintQueueItem)
                    .where(PrintQueueItem.id.in_(ids), PrintQueueItem.queue_id == queue_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            }
            # Foreign rows are never locked under this lane. The result exposes
            # no foreign state, and cannot mutate or steal a different queue's job.
            blobs = {
                blob.id: blob
                for blob in await db.scalars(
                    select(QueueSource)
                    .where(QueueSource.id.in_({row.queue_source_id for row in rows.values() if row.queue_source_id}))
                    .execution_options(populate_existing=True)
                )
            }
            left = []
            eligible = []
            for item_id in ids:
                row = rows.get(item_id)
                reason = (
                    "missing"
                    if row is None
                    else "not_failed"
                    if row.status != ("cancelled" if item_id in cancelled else "failed")
                    else "source_unavailable"
                    if row.queue_source_id
                    and (row.queue_source_id not in blobs or blobs[row.queue_source_id].state != STATE_READY)
                    else None
                )
                if reason:
                    left.append({"id": item_id, "reason": reason})
                else:
                    eligible.append(row)
            position = (
                await db.scalar(
                    select(func.max(PrintQueueItem.position)).where(
                        PrintQueueItem.queue_id == queue_id, PrintQueueItem.status == "pending"
                    )
                )
                or 0
            )
            retried = []
            for row in sorted(eligible, key=lambda item: (item.position, item.id)):
                position += 1
                restore_routing_source(row)
                row.status = "pending"
                row.position = position
                row.error_message = None
                row.error_code = None
                row.completed_at = None
                # The operator's Retry is a new intent, so a new bounded budget (owner,
                # 2026-10-23). ``started_at`` stays: it is the proof this row was
                # dispatched before, which ``swap_job_refresh`` reads.
                row.dispatch_attempts = 0
                retried.append(row.id)
            await update_queue_counters(db, queue_id)
            await db.commit()
    except BaseException:
        await db.rollback()
        raise
    from backend.app.services import dispatch_stall_tracker

    for item_id in retried:
        # A new intent: what this row fails at from now on is a new stall episode, never the old one's tail.
        dispatch_stall_tracker.new_incarnation(item_id)
    return {"retried": retried, "left_in_place": left, "not_attempted": []}
