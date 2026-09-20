"""Queue operation helpers.

Batch-aware reorder / bump / clone / status transitions for print queue
items.  Pure async functions — no FastAPI types, no logging beyond info.
Used by the new queue command endpoints.

⚠️ **No FastAPI types** is why a clone whose bytes are no longer printable raises
the capture service's own ``QueueSourceError`` from here and the route turns it
into a status with ``queue_source_capture.refusal`` — one taxonomy, mapped once
(spec §6).
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.queue_source_capture import reusing_sources

logger = logging.getLogger(__name__)


@asynccontextmanager
async def queue_scope_lock(db: AsyncSession, queue_id: int):
    """Serialize a short mutation or claim for one printer queue.

    PostgreSQL needs an explicit transaction-scoped lock because two writers
    otherwise both observe the same pending order. SQLite already serializes a
    short write transaction, so it deliberately takes no process-local lock.
    Callers must keep file I/O, MQTT and other long work outside this scope.
    """
    if db.get_bind().dialect.name == "postgresql":
        # 1625 is the existing namespace used for per-queue append locks.
        await db.execute(text("SELECT pg_advisory_xact_lock(1625, :queue_id)"), {"queue_id": queue_id})
    yield


async def place_pending_block(
    db: AsyncSession,
    queue_id: int,
    new_items: list[PrintQueueItem],
    *,
    enqueue_position: str = "end",
) -> None:
    """Assign one new pending block at the end or before existing pending work.

    The caller holds :func:`queue_scope_lock`. ``next`` reindexes every pending
    row to make its block deterministic; ``end`` preserves legacy positions
    and only assigns positions to the new rows. Terminal and printing rows are
    never read or moved.
    """
    if enqueue_position not in {"end", "next"}:
        raise ValueError(f"Unsupported enqueue position: {enqueue_position}")

    existing = await _pending_items_in_queue(db, queue_id)
    if enqueue_position == "end":
        # ⚠️ ``default=0``, so the first pending row of an empty queue is 1, not 0.
        # Position 0 is the direct-print claim's slot (``queue_batch``: "position=0
        # keeps it out of the pending ordering"); letting pending work start there
        # too costs that row its one distinguishing mark, and every listing that
        # orders by ``(position, id)`` without filtering on status then sorts a
        # live claim against fresh pending work by insertion order.
        next_position = max((item.position for item in existing), default=0) + 1
        for offset, item in enumerate(new_items):
            item.position = next_position + offset
        return

    ordered = [*new_items, *existing]
    for position, item in enumerate(ordered):
        item.position = position


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
        .order_by(PrintQueueItem.position, PrintQueueItem.id)
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
        .order_by(PrintQueueItem.position, PrintQueueItem.id)
    )
    return list(result.scalars().all())


async def reorder_block(db: AsyncSession, queue_id: int, block_ids: list[int], direction: str) -> int:
    """Move a block while serializing with all other queue writers."""
    async with queue_scope_lock(db, queue_id):
        return await _reorder_block_unlocked(db, queue_id, block_ids, direction)


async def _reorder_block_unlocked(db: AsyncSession, queue_id: int, block_ids: list[int], direction: str) -> int:
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
    """Move a block to the top while serializing with queue writers."""
    async with queue_scope_lock(db, queue_id):
        return await _bump_block_to_top_unlocked(db, queue_id, block_ids)


async def _bump_block_to_top_unlocked(db: AsyncSession, queue_id: int, block_ids: list[int]) -> int:
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
    """Move a block to the bottom while serializing with queue writers."""
    async with queue_scope_lock(db, queue_id):
        return await _bump_block_to_bottom_unlocked(db, queue_id, block_ids)


async def _bump_block_to_bottom_unlocked(db: AsyncSession, queue_id: int, block_ids: list[int]) -> int:
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
    """Shallow clone of a queue item's user-editable fields.

    ⚠️ **Every print option belongs here, and the list rots if nobody watches
    it.** It once carried 19 of the model's columns and had not grown since:
    the tri-state calibration modes (m106), the preheat overrides (m103),
    ``require_previous_success`` (m116), gcode injection, the H2C nozzle
    mapping and the selected macros were all dropped, so a cloned or retried
    job printed with different settings than the one it copied — silently,
    which is worse than refusing to clone at all.
    ``test_queue_ops.TestACloneCarriesEveryPrintOption`` now fails when a new
    column is added and forgotten; it also pins what is deliberately NOT
    carried, and why.
    """
    item = PrintQueueItem(
        queue_id=src.queue_id,
        archive_id=src.archive_id,
        library_file_id=src.library_file_id,
        # m173: the clone prints the SAME bytes and becomes a second owner of the
        # same blob — never a new read of the original (queue-source-spool spec
        # §9). The snapshot travels beside the id because that is what keeps the
        # display name and the plate fallback with the copy.
        queue_source_id=src.queue_source_id,
        source_snapshot=src.source_snapshot,
        project_id=src.project_id,
        project_line_id=src.project_line_id,
        # Carried, not reset to "queue": a retry of an external print is still
        # that same print being done again, and must stay as quiet about the
        # queue as the original was.
        origin=src.origin,
        position=new_position,
        scheduled_time=src.scheduled_time,
        manual_start=src.manual_start,
        auto_off_after=src.auto_off_after,
        require_previous_success=src.require_previous_success,
        ams_mapping=src.ams_mapping,
        filament_routing=src.filament_routing,
        nozzle_mapping=src.nozzle_mapping,
        plate_id=src.plate_id,
        bed_levelling=src.bed_levelling,
        bed_levelling_mode=src.bed_levelling_mode,
        flow_cali=src.flow_cali,
        flow_cali_mode=src.flow_cali_mode,
        nozzle_offset_cali=src.nozzle_offset_cali,
        nozzle_offset_cali_mode=src.nozzle_offset_cali_mode,
        layer_inspect=src.layer_inspect,
        timelapse=src.timelapse,
        timelapse_storage=src.timelapse_storage,
        use_ams=src.use_ams,
        mesh_mode_fast_check=src.mesh_mode_fast_check,
        gcode_injection=src.gcode_injection,
        execute_swap_macros=src.execute_swap_macros,
        swap_macro_events=src.swap_macro_events,
        selected_macro_ids=src.selected_macro_ids,
        preheat_override=src.preheat_override,
        preheat_chamber_target_override=src.preheat_chamber_target_override,
        status="pending",
        batch_id=new_batch_id,
        created_by_id=src.created_by_id,
    )

    from backend.app.services.filament_policy import restore_routing_source

    restore_routing_source(item)
    return item


async def clone_item(db: AsyncSession, item_id: int, keep_batch: bool = True) -> PrintQueueItem | None:
    """Insert a duplicate after *item_id*.

    ``keep_batch=True`` shares ``batch_id`` — new copy becomes a sibling
    in the same batch.  ``keep_batch=False`` creates a solo item with
    ``batch_id=NULL``.

    ⚠️ m173: the copy becomes a **second owner of the same blob** (spec §9), so the
    row is written inside :func:`reusing_sources` — asked under the storage guard,
    then written under it. A blob that is not ``ready`` refuses the clone instead of
    producing a row that could only ever fail at dispatch.
    """
    src = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if src is None:
        return None

    new_batch_id = src.batch_id if keep_batch else None
    async with reusing_sources(db, [src.queue_source_id]), queue_scope_lock(db, src.queue_id):
        # New copy appended to end of its queue.
        max_pos = (
            await db.execute(
                select(func.max(PrintQueueItem.position))
                .where(PrintQueueItem.queue_id == src.queue_id)
                .where(PrintQueueItem.status == "pending")
            )
        ).scalar() or 0
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

    ⚠️ **Every sibling's blob**, in one acquisition of the guard: a batch normally
    shares one source, but nothing in the model requires it (rows can be grouped
    into a batch after the fact), and a per-row guard would let the collector act
    between two copies of the same batch.

    ⚠️ The ``refresh`` loop is deliberately **outside** the guard. The rows are
    already committed by then and no refresh can affect who owns a blob, while the
    guard is the most contended lock in the process and every holder of it is
    already bounded by SQLite's 15-second busy timeout on the commit above (the
    real bound on a hold, not the batch size). One reload per clone under that lock
    buys nothing.
    """
    siblings = await get_batch_pending_items(db, batch_id)
    if not siblings:
        return []

    new_batch_id = str(uuid.uuid4())
    queue_id = siblings[0].queue_id
    clones: list[PrintQueueItem] = []
    async with reusing_sources(db, [src.queue_source_id for src in siblings]), queue_scope_lock(db, queue_id):
        max_pos = (
            await db.execute(
                select(func.max(PrintQueueItem.position))
                .where(PrintQueueItem.queue_id == queue_id)
                .where(PrintQueueItem.status == "pending")
            )
        ).scalar() or 0
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
