"""Queue operation helpers.

Batch-aware reorder / bump / clone / status transitions for print queue
items.  Pure async functions — no FastAPI types, no logging beyond info.
Used by the new queue command endpoints.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem

logger = logging.getLogger(__name__)


@dataclass
class CloneScope:
    single = "single"
    batch = "batch"


async def get_batch_pending_items(db: AsyncSession, batch_id: str) -> list[PrintQueueItem]:
    """All pending items sharing this batch_id, ordered by position.

    Items in ``printing``/``completed``/``failed``/``cancelled`` status
    are intentionally excluded — batch operations only touch pending.
    """
    result = await db.execute(
        select(PrintQueueItem)
        .where(PrintQueueItem.batch_id == batch_id)
        .where(PrintQueueItem.status == "pending")
        .order_by(PrintQueueItem.position)
    )
    return list(result.scalars().all())


async def resolve_block_ids(db: AsyncSession, item_id: int) -> tuple[int, list[int]]:
    """Return ``(queue_id, ids)`` — all items that move together as a unit.

    * Solo item (``batch_id`` is NULL) → ``[item_id]``
    * Batched item → all pending siblings' ids (including *item_id*).
    """
    item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if item is None:
        return 0, []

    if not item.batch_id:
        return item.queue_id, [item.id]

    siblings = await get_batch_pending_items(db, item.batch_id)
    return item.queue_id, [s.id for s in siblings]


async def _pending_items_in_queue(db: AsyncSession, queue_id: int) -> list[PrintQueueItem]:
    result = await db.execute(
        select(PrintQueueItem)
        .where(PrintQueueItem.queue_id == queue_id)
        .where(PrintQueueItem.status == "pending")
        .order_by(PrintQueueItem.position)
    )
    return list(result.scalars().all())


async def reorder_block(db: AsyncSession, queue_id: int, block_ids: list[int], direction: str) -> int:
    """Move a contiguous or non-contiguous *block* one step up/down.

    Algorithm: find the anchor position of the block (min position of
    any block item going up, max going down), swap position with the
    closest non-block pending item in the requested direction.

    Returns the number of items whose position actually changed
    (0 when the block is already at the boundary).
    """
    if direction not in ("up", "down"):
        raise ValueError(f"bad direction: {direction}")
    if not block_ids:
        return 0

    pending = await _pending_items_in_queue(db, queue_id)
    id_to_item = {it.id: it for it in pending}
    block_set = {i for i in block_ids if i in id_to_item}
    if not block_set:
        return 0

    non_block = [it for it in pending if it.id not in block_set]
    if not non_block:
        return 0  # nothing to swap with

    if direction == "up":
        anchor = min(id_to_item[i].position for i in block_set)
        swap_candidates = [it for it in non_block if it.position < anchor]
        if not swap_candidates:
            return 0
        swap_with = max(swap_candidates, key=lambda x: x.position)
        new_block_pos = swap_with.position
        # Swap: block moves to swap_with's position, swap_with moves to
        # where the first block item was.
        old_min = anchor
        for i in block_set:
            id_to_item[i].position = id_to_item[i].position - (old_min - new_block_pos)
        swap_with.position = old_min + (len(block_set) - 1)
    else:  # down
        anchor = max(id_to_item[i].position for i in block_set)
        swap_candidates = [it for it in non_block if it.position > anchor]
        if not swap_candidates:
            return 0
        swap_with = min(swap_candidates, key=lambda x: x.position)
        delta = swap_with.position - anchor
        for i in block_set:
            id_to_item[i].position = id_to_item[i].position + delta
        swap_with.position = anchor - (len(block_set) - 1)

    await db.commit()
    logger.info("Queue %s: moved block %s %s", queue_id, block_ids, direction)
    return len(block_set) + 1


async def bump_block_to_top(db: AsyncSession, queue_id: int, block_ids: list[int]) -> int:
    """Move the block to the very top (lowest positions) of the queue.

    Preserves intra-block order.  Returns how many items shifted.
    """
    if not block_ids:
        return 0
    pending = await _pending_items_in_queue(db, queue_id)
    id_to_item = {it.id: it for it in pending}
    block_items = [id_to_item[i] for i in block_ids if i in id_to_item]
    if not block_items:
        return 0
    non_block = [it for it in pending if it.id not in {b.id for b in block_items}]
    block_items.sort(key=lambda it: it.position)

    # Already at top?
    if block_items[0].position == 0 and all(block_items[i].position == i for i in range(len(block_items))):
        return 0

    # Assign new positions: block first (preserving intra-order), then
    # everyone else in their existing order.
    next_pos = 0
    for it in block_items:
        it.position = next_pos
        next_pos += 1
    for it in non_block:
        it.position = next_pos
        next_pos += 1

    await db.commit()
    logger.info("Queue %s: bumped block %s to top", queue_id, block_ids)
    return len(block_items) + len(non_block)


async def bump_block_to_bottom(db: AsyncSession, queue_id: int, block_ids: list[int]) -> int:
    """Move the block to the very bottom (highest positions) of the queue.

    Preserves intra-block order.  Returns how many items shifted.
    """
    if not block_ids:
        return 0
    pending = await _pending_items_in_queue(db, queue_id)
    id_to_item = {it.id: it for it in pending}
    block_items = [id_to_item[i] for i in block_ids if i in id_to_item]
    if not block_items:
        return 0
    non_block = [it for it in pending if it.id not in {b.id for b in block_items}]
    block_items.sort(key=lambda it: it.position)

    # Already at bottom?
    total = len(pending)
    if block_items[-1].position == total - 1 and all(
        block_items[-1 - i].position == total - 1 - i for i in range(len(block_items))
    ):
        return 0

    # Assign new positions: non-block items keep their relative order at the
    # top, then the block (preserving intra-order) fills the tail.
    next_pos = 0
    for it in non_block:
        it.position = next_pos
        next_pos += 1
    for it in block_items:
        it.position = next_pos
        next_pos += 1

    await db.commit()
    logger.info("Queue %s: bumped block %s to bottom", queue_id, block_ids)
    return len(block_items) + len(non_block)


def _copy_item_fields(src: PrintQueueItem, new_batch_id: str | None, new_position: int) -> PrintQueueItem:
    """Shallow clone of a queue item's user-editable fields."""
    return PrintQueueItem(
        queue_id=src.queue_id,
        archive_id=src.archive_id,
        library_file_id=src.library_file_id,
        project_id=src.project_id,
        position=new_position,
        scheduled_time=src.scheduled_time,
        manual_start=src.manual_start,
        auto_off_after=src.auto_off_after,
        ams_mapping=src.ams_mapping,
        plate_id=src.plate_id,
        bed_levelling=src.bed_levelling,
        flow_cali=src.flow_cali,
        layer_inspect=src.layer_inspect,
        timelapse=src.timelapse,
        use_ams=src.use_ams,
        mesh_mode_fast_check=src.mesh_mode_fast_check,
        execute_swap_macros=src.execute_swap_macros,
        swap_macro_events=src.swap_macro_events,
        status="pending",
        batch_id=new_batch_id,
        created_by_id=src.created_by_id,
    )


async def clone_item(db: AsyncSession, item_id: int, keep_batch: bool = True) -> PrintQueueItem | None:
    """Insert a duplicate after *item_id*.

    ``keep_batch=True`` shares ``batch_id`` — new copy becomes a sibling
    in the same batch.  ``keep_batch=False`` creates a solo item with
    ``batch_id=NULL``.
    """
    src = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if src is None:
        return None

    # New copy appended to end of its queue.
    max_pos = (
        await db.execute(
            select(func.max(PrintQueueItem.position))
            .where(PrintQueueItem.queue_id == src.queue_id)
            .where(PrintQueueItem.status == "pending")
        )
    ).scalar() or 0

    new_batch_id = src.batch_id if keep_batch else None
    clone = _copy_item_fields(src, new_batch_id, max_pos + 1)
    db.add(clone)
    await db.commit()
    await db.refresh(clone)
    logger.info("Cloned queue item %s → %s (keep_batch=%s)", src.id, clone.id, keep_batch)
    return clone


async def clone_batch(db: AsyncSession, batch_id: str) -> list[PrintQueueItem]:
    """Create a fresh batch (new batch_id) duplicating every pending item
    in the source batch.  Copies appended to end of queue, preserve
    intra-batch order.
    """
    siblings = await get_batch_pending_items(db, batch_id)
    if not siblings:
        return []

    new_batch_id = str(uuid.uuid4())
    queue_id = siblings[0].queue_id
    max_pos = (
        await db.execute(
            select(func.max(PrintQueueItem.position))
            .where(PrintQueueItem.queue_id == queue_id)
            .where(PrintQueueItem.status == "pending")
        )
    ).scalar() or 0

    clones: list[PrintQueueItem] = []
    for i, src in enumerate(siblings):
        clone = _copy_item_fields(src, new_batch_id, max_pos + 1 + i)
        db.add(clone)
        clones.append(clone)

    await db.commit()
    for c in clones:
        await db.refresh(c)
    logger.info("Cloned batch %s into new batch %s (%d items)", batch_id, new_batch_id, len(clones))
    return clones


async def set_status(db: AsyncSession, item_id: int, new_status: str) -> bool:
    """Set status on a single item.  Returns True if changed."""
    item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if item is None or item.status == new_status:
        return False
    item.status = new_status
    await db.commit()
    return True


async def set_status_for_batch(db: AsyncSession, batch_id: str, new_status: str) -> int:
    """Apply new_status to all pending items in batch.  Returns count."""
    pending = await get_batch_pending_items(db, batch_id)
    for it in pending:
        it.status = new_status
    if pending:
        await db.commit()
    return len(pending)
