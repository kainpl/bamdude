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

import asyncio
import logging
import uuid
from collections import Counter, defaultdict
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import dataclass

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.filament_intake import snapshot_for_copy
from backend.app.services.queue_source_capture import reusing_sources

logger = logging.getLogger(__name__)

_queue_claim_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

_HELD_LANES = "g03_held_lanes"


def held_lanes(db: AsyncSession) -> frozenset[int]:
    """The printer lanes this session's writer holds now (G03 B, A20): entered by ``claim_lanes``,
    ``queue_claim_scope`` or ``queue_scope_lock`` and left with them. A helper that would write a printer row of
    another lane asks this and refuses -- the lanes are taken before the rows, never late."""
    info = getattr(db, "info", None)
    held = info.get(_HELD_LANES, {}) if isinstance(info, dict) else {}
    return frozenset(lane for lane, count in held.items() if count > 0)


@contextmanager
def _holding(db: AsyncSession, lanes):
    info = getattr(db, "info", None)
    if not isinstance(info, dict):  # a stand-in session (lock-order probes, mocks) records nothing
        yield
        return
    held = info.setdefault(_HELD_LANES, Counter())
    held.update(lanes)
    try:
        yield
    finally:
        held.subtract(lanes)
        for lane in [lane for lane, count in held.items() if count <= 0]:
            del held[lane]


@asynccontextmanager
async def queue_scope_lock(db: AsyncSession, queue_id: int):
    """Serialize a short mutation or claim for one printer queue.

    PostgreSQL needs an explicit transaction-scoped lock because two writers
    otherwise both observe the same pending order. SQLite already serializes a
    short write transaction, so it deliberately takes no process-local lock --
    but it takes SQLite's writer HERE, before the body reads (G03 B, A15): pysqlite
    opens a transaction only at the first write, so the positions, the header and
    the head a body read before its first write were read beside a claim or a
    verdict holding the lane, and the write then landed on what they had changed
    (TA19: a reorder read its neighbours while a verdict skipped one of them). No
    body under this scope waits for an in-process lock, so holding the writer
    through it cannot meet a lane holder waiting for SQLite's writer.
    Callers must keep file I/O, MQTT and other long work outside this scope.
    """
    if db.get_bind().dialect.name == "postgresql":
        # 1625 is the existing namespace used for per-queue append locks.
        await db.execute(text("SELECT pg_advisory_xact_lock(1625, :queue_id)"), {"queue_id": queue_id})
    else:
        from backend.app.core.database import take_write_lock
        from backend.app.models.printer_queue import PrinterQueue

        await take_write_lock(db, PrinterQueue.__table__, queue_id)
    with _holding(db, [queue_id]):
        yield


@asynccontextmanager
async def queue_claim_scope(db: AsyncSession, queue_id: int, *, process_held: bool = False):
    """Serialize a short final printer-lane admission.

    SQLite's in-process lock closes the coroutine window. A fresh SQLite
    session also takes its writer before authoritative reads; a caller-owned
    publication/write transaction is preserved. PostgreSQL's advisory guard
    remains transaction-scoped through the caller's commit; callers must keep
    the critical section short and do no external I/O in it.
    """
    from contextlib import nullcontext

    async with nullcontext() if process_held else _queue_claim_locks[queue_id]:
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            await _lane_db_guard(db, queue_id)
        elif dialect == "sqlite" and not db.in_transaction():
            await db.execute(text("BEGIN IMMEDIATE"))
        with _holding(db, [queue_id]):
            yield


def lane_held_in_process(db: AsyncSession, queue_id: int) -> bool:
    """SQLite only: whether another coroutine holds this printer lane's in-process lock right now.

    A caller whose transaction may already hold SQLite's writer (the AutoQueue tick) must not wait for that lock: its
    holder -- a refresh, a change_table claim, a scheduler claim -- takes the writer next (``claim_lanes``), and each
    would sit on the other until SQLite's busy timeout. Checked and then entered with no await in between, so the
    answer still holds when the caller enters ``queue_claim_scope``.
    """
    return db.get_bind().dialect.name == "sqlite" and _queue_claim_locks[queue_id].locked()


async def _lane_db_guard(db: AsyncSession, queue_id: int) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock(1625, :queue_id)"), {"queue_id": queue_id})


@asynccontextmanager
async def queue_process_lanes(queue_ids):
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as guards:
        for queue_id in sorted(set(queue_ids)):
            await guards.enter_async_context(_queue_claim_locks[queue_id])
        yield


@asynccontextmanager
async def claim_lanes(db: AsyncSession, queue_ids, *, archive_id: int | None = None, archive_ids=(), before_db=None):
    """The one shared order for a writer that needs printer lanes AND archives (spec r4 §4, T9.7 R4).

    In-process locks, ALL of them before SQLite's writer: the archives ascending, then the lanes ascending. Then
    the database: SQLite's writer, or on PostgreSQL the archives' guards ascending, then the lanes' guards
    ascending. A library-source writer uses before_db to lock its library rows
    and early product/order footprint before these DB guards; then print rows,
    then queue/job rows. Archive-only callers keep archive-before-jobs. Archive before queue is the order every archive + queue writer keeps: the terminal acceptance, an
    archive's hard delete, the change_table claim, a refresh of an archive's jobs, operator print-state recovery
    and the terminal autorepair.

    A holder of SQLite's single writer must never wait for an in-process lock -- whoever holds that lock takes the
    writer next, and both would sit until SQLite's busy timeout failed one of them (plan T9.4). The set of lanes is
    the caller's preflight; a lane found under these locks that is not in it is a refusal, never a lock taken late.
    """
    from contextlib import AsyncExitStack

    from backend.app.services.archive_write_scope import archive_db_guard, archive_process_lock

    archives = sorted({*archive_ids, *([archive_id] if archive_id is not None else [])})
    lanes = sorted(set(queue_ids))
    async with AsyncExitStack() as stack:
        for each in archives:
            await stack.enter_async_context(archive_process_lock(each))
        for queue_id in lanes:
            await stack.enter_async_context(_queue_claim_locks[queue_id])
        if before_db is not None:
            await before_db(db)
        dialect = db.get_bind().dialect.name
        if dialect == "sqlite" and not db.in_transaction():
            await db.execute(text("BEGIN IMMEDIATE"))
        if dialect == "postgresql":
            for each in archives:
                await archive_db_guard(db, each)
            for queue_id in lanes:
                await _lane_db_guard(db, queue_id)
        with _holding(db, lanes):
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


#: The rows of a batch that are still in a printer queue (ungroup reaches both).
ACTIVE_BATCH_STATUSES = ("pending", "printing")


class BatchSpansQueues(ValueError):
    """A batch action named no queue, and the batch's active rows sit in several (G03 D1).

    AutoQueue promotion copies the router row's ``batch_id`` onto every printer row it creates, so one
    batch normally spreads over the printers that took its copies. A batch action belongs to the queue the
    operator is looking at; without one named, acting on every printer's copies at once is refused rather
    than done by accident.
    """

    def __init__(self, queue_ids):
        super().__init__(f"batch spans queues {sorted(queue_ids)}")
        self.queue_ids = sorted(queue_ids)


async def batch_queue_id(db: AsyncSession, batch_id: str, queue_id: int | None) -> int | None:
    """The one printer queue a batch action acts on (G03 D1, owner decision 2026-11-19).

    A named queue is the answer. Without one: the queue holding the batch's active rows (pending or
    printing) when there is exactly one, ``None`` when there are none, and :class:`BatchSpansQueues` when
    they span several. Finished rows never decide it -- history keeps its grouping wherever it printed.
    """
    if queue_id is not None:
        return queue_id
    queues = set(
        (
            await db.scalars(
                select(PrintQueueItem.queue_id)
                .where(PrintQueueItem.batch_id == batch_id, PrintQueueItem.status.in_(ACTIVE_BATCH_STATUSES))
                .distinct()
            )
        ).all()
    )
    if len(queues) > 1:
        raise BatchSpansQueues(queues)
    return next(iter(queues), None)


async def get_batch_pending_items(
    db: AsyncSession, batch_id: str, *, queue_id: int | None = None
) -> list[PrintQueueItem]:
    """All pending items sharing this batch_id, ordered by position.

    Items in ``printing``/``completed``/``failed``/``cancelled`` status
    are intentionally excluded — batch operations only touch pending.
    ``queue_id`` keeps the rows of one printer queue: a batch spread over
    several printers by AutoQueue is acted on per queue (G03 D1).
    """
    stmt = (
        select(PrintQueueItem)
        .where(PrintQueueItem.batch_id == batch_id)
        .where(PrintQueueItem.status == "pending")
        .order_by(PrintQueueItem.position, PrintQueueItem.id)
    )
    if queue_id is not None:
        stmt = stmt.where(PrintQueueItem.queue_id == queue_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def resolve_block_ids(db: AsyncSession, item_id: int) -> tuple[int, list[int]]:
    """Return ``(queue_id, ids)`` — all items that move together as a unit.

    * Solo item (``batch_id`` is NULL) → ``[item_id]``
    * Batched item → its pending siblings' ids in ITS queue (including *item_id*):
      the copies AutoQueue gave other printers are their queues' work (G03 D1).
    """
    item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if item is None:
        return 0, []

    if not item.batch_id:
        return item.queue_id, [item.id]

    siblings = await get_batch_pending_items(db, item.batch_id, queue_id=item.queue_id)
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
    wanted = set(block_ids)
    # In the queue's own order (position, then id), so a tie cannot reorder the block.
    block_items = [it for it in pending if it.id in wanted]
    if not block_items:
        return 0
    non_block = [it for it in pending if it.id not in wanted]

    # Already at bottom? Asked of the ORDER, never of the position values: pending positions start at 1
    # (``place_pending_block``), and a check that assumed 0..n-1 took a block sitting just above the last
    # row for one already at the bottom -- the bump did nothing (G03 follow-up).
    if [it.id for it in pending[-len(block_items) :]] == [it.id for it in block_items]:
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
        # display name and the plate fallback with the copy; the async callers
        # replace it with ``snapshot_for_copy``, which records the declaration.
        queue_source_id=src.queue_source_id,
        source_snapshot=src.source_snapshot,
        project_id=src.project_id,
        project_line_id=src.project_line_id,
        # Carried, not reset to "queue": a retry of an external print is still
        # that same print being done again, and must stay as quiet about the
        # queue as the original was.
        origin=src.origin,
        auto_eject=src.auto_eject,
        auto_eject_settings=deepcopy(src.auto_eject_settings),
        position=new_position,
        scheduled_time=src.scheduled_time,
        manual_start=src.manual_start,
        auto_off_after=src.auto_off_after,
        require_previous_success=src.require_previous_success,
        ams_mapping=src.ams_mapping,
        filament_routing=src.filament_routing,
        nozzle_mapping=src.nozzle_mapping,
        nozzle_rack_choice=src.nozzle_rack_choice,
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

    from backend.app.services.filament_policy import clear_routing_runtime

    # The copy's source link is the one rule's (G03 B, A27): the async callers write it after locking the
    # candidate's parent; here it carries the source row's link only until they do.
    clear_routing_runtime(item)
    return item


def _as_new_work(clone: PrintQueueItem, created_by_id: int | None | str, keep_order: bool) -> None:
    """A clone is new work (WS-13 E13 Q-02): its creator is whoever cloned it, and without
    ``keep_order`` it carries no order. ``"source"`` keeps the source's creator (internal callers)."""
    if created_by_id != "source":
        clone.created_by_id = created_by_id
    if not keep_order:
        clone.project_id = None
        clone.project_line_id = None


async def clone_item(
    db: AsyncSession,
    item_id: int,
    keep_batch: bool = True,
    *,
    created_by_id: int | None | str = "source",
    keep_order: bool = True,
    authority=None,
) -> PrintQueueItem | None:
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

    from backend.app.services.print_execution import require_repeatable
    from backend.app.services.queue_sources import SourceUnreadable

    await require_repeatable(db, src, whole_batch=True)
    pin = (src.recovery_identity, src.queue_id, src.queue_source_id, src.batch_id)
    new_batch_id = src.batch_id if keep_batch else None
    from backend.app.services import queue_source_link
    from backend.app.services.publication_authority import queue_item_access

    # A clone is an insert: its key is always checked, so its parent is always taken (A27 rule 2, H3).
    link_plan = queue_source_link.plan_link(src, insert=True)
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.printer_occupancy import queue_header_holds

    queue_identity = await db.scalar(select(PrinterQueue.recovery_identity).where(PrinterQueue.id == src.queue_id))

    async with reusing_sources(
        db,
        [src.queue_source_id],
        queue_ids=[src.queue_id],
        authority=authority,
        printer_id=src.queue_id,
        source_access=queue_item_access([src]),
    ):
        await queue_source_link.lock_plans(db, [link_plan])
        src = await db.get(PrintQueueItem, item_id, populate_existing=True, with_for_update={"key_share": True})
        if src is None or (src.recovery_identity, src.queue_id, src.queue_source_id, src.batch_id) != pin:
            raise SourceUnreadable("clone source changed")
        if not await queue_header_holds(db, src.queue_id, queue_identity):  # A25
            raise SourceUnreadable("clone source changed")
        await require_repeatable(db, src, whole_batch=True)
        if created_by_id != "source" and authority is not None:
            from backend.app.services.publication_authority import current_creator

            creator = await current_creator(db, authority)
            created_by_id = creator.id if creator is not None else None
        # New copy appended to end of its queue.
        max_pos = (
            await db.execute(
                select(func.max(PrintQueueItem.position))
                .where(PrintQueueItem.queue_id == src.queue_id)
                .where(PrintQueueItem.status == "pending")
            )
        ).scalar() or 0
        clone = _copy_item_fields(src, new_batch_id, max_pos + 1)
        if not queue_source_link.apply_link(clone, link_plan, source=src):
            raise SourceUnreadable("clone source changed")
        clone.source_snapshot = await snapshot_for_copy(db, src)
        _as_new_work(clone, created_by_id, keep_order)
        db.add(clone)
        await db.commit()
        await db.refresh(clone)
    logger.info("Cloned queue item %s → %s (keep_batch=%s)", src.id, clone.id, keep_batch)
    return clone


async def clone_batch(
    db: AsyncSession,
    batch_id: str,
    *,
    queue_id: int | None = None,
    created_by_id: int | None | str = "source",
    keep_order: bool = True,
    authority=None,
) -> list[PrintQueueItem]:
    """Create a fresh batch (new batch_id) duplicating every pending item
    of the source batch in ONE printer queue.  Copies appended to end of
    that queue, preserve intra-batch order.

    ⚠️ **One queue** (G03 D1): ``queue_id``, or the batch's only queue —
    :class:`BatchSpansQueues` when its active rows sit in several. The
    positions, the lane and the counters are that queue's; cloning another
    printer's copies here gave them positions of a queue they were not in.

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
    all_siblings = list((await db.scalars(select(PrintQueueItem).where(PrintQueueItem.batch_id == batch_id))).all())
    from backend.app.services.print_execution import require_repeatable

    for member in all_siblings:
        await require_repeatable(db, member, whole_batch=True)
    queue_id = await batch_queue_id(db, batch_id, queue_id)
    if queue_id is None:
        return []
    siblings = await get_batch_pending_items(db, batch_id, queue_id=queue_id)
    if not siblings:
        return []
    pins = tuple((row.id, row.recovery_identity, row.queue_source_id, row.queue_id) for row in siblings)

    new_batch_id = str(uuid.uuid4())
    clones: list[PrintQueueItem] = []
    from backend.app.services import queue_source_link
    from backend.app.services.publication_authority import queue_item_access

    link_plans = {row.id: queue_source_link.plan_link(row, insert=True) for row in siblings}
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.printer_occupancy import queue_header_holds

    queue_identity = await db.scalar(select(PrinterQueue.recovery_identity).where(PrinterQueue.id == queue_id))

    async with reusing_sources(
        db,
        [src.queue_source_id for src in siblings],
        queue_ids=[queue_id],
        authority=authority,
        printer_id=queue_id,
        source_access=queue_item_access(siblings),
    ):
        await queue_source_link.lock_plans(db, list(link_plans.values()))
        siblings = await get_batch_pending_items(db, batch_id, queue_id=queue_id)
        if tuple((row.id, row.recovery_identity, row.queue_source_id, row.queue_id) for row in siblings) != pins:
            from backend.app.services.queue_sources import SourceUnreadable

            raise SourceUnreadable("clone batch changed")
        if not await queue_header_holds(db, queue_id, queue_identity):  # A25
            from backend.app.services.queue_sources import SourceUnreadable

            raise SourceUnreadable("clone batch changed")
        for member in siblings:
            await require_repeatable(db, member, whole_batch=True)
        if created_by_id != "source" and authority is not None:
            from backend.app.services.publication_authority import current_creator

            creator = await current_creator(db, authority)
            created_by_id = creator.id if creator is not None else None
        max_pos = (
            await db.execute(
                select(func.max(PrintQueueItem.position))
                .where(PrintQueueItem.queue_id == queue_id)
                .where(PrintQueueItem.status == "pending")
            )
        ).scalar() or 0
        for i, src in enumerate(siblings):
            clone = _copy_item_fields(src, new_batch_id, max_pos + 1 + i)
            if not queue_source_link.apply_link(clone, link_plans[src.id], source=src):
                from backend.app.services.queue_sources import SourceUnreadable

                raise SourceUnreadable("clone batch changed")
            clone.source_snapshot = await snapshot_for_copy(db, src)
            _as_new_work(clone, created_by_id, keep_order)
            db.add(clone)
            clones.append(clone)

        await db.commit()
    for c in clones:
        await db.refresh(c)
    logger.info("Cloned batch %s into new batch %s (%d items)", batch_id, new_batch_id, len(clones))
    return clones


@asynccontextmanager
async def lane_session(queue_ids):
    """A fresh session under ``claim_lanes(sorted(queues))`` — the shape of every light writer (G03 B, A7, L1).

    ``queue_ids`` are read off the named rows before the guards; a row found under them in another queue is the
    writer's refusal, never a lock taken late.
    """
    from backend.app.core import database

    async with database.async_session() as db, claim_lanes(db, sorted(set(queue_ids))):
        yield db


async def lock_named_rows(db: AsyncSession, item_ids) -> dict[int, PrintQueueItem]:
    """The named rows ``FOR NO KEY UPDATE`` in ascending id, re-read — inside the writer's lanes."""
    rows = await db.scalars(
        select(PrintQueueItem)
        .where(PrintQueueItem.id.in_(sorted(set(item_ids))))
        .order_by(PrintQueueItem.id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    return {row.id: row for row in rows}


async def set_status(
    db: AsyncSession,
    item_id: int,
    new_status: str,
    *,
    completed_at=None,
    commit: bool = True,
    queue_id: int | None = None,
) -> bool:
    """Move one PENDING item to ``new_status``.  Returns True if it changed.

    ⚠️ A compare-and-set on ``status='pending'`` (G03 E-2): the route read the row a moment
    earlier, and the scheduler may have claimed it since. An ORM write from that read would
    overwrite ``printing`` and leave the printer's claim behind; the Core ``UPDATE`` simply
    matches nothing. ``completed_at`` is stamped by a caller that ends the row.
    """
    if new_status == "pending":
        return False
    # Every write of ``status`` voids the deferral proof (G03 B, A16).
    values: dict = {"status": new_status, "deferred_claim_token": None}
    if completed_at is not None:
        values["completed_at"] = completed_at
    conditions = [PrintQueueItem.id == item_id, PrintQueueItem.status == "pending"]
    if queue_id is not None:
        conditions.append(PrintQueueItem.queue_id == queue_id)  # the lane the caller holds (A7)
    changed = (
        await db.execute(update(PrintQueueItem).where(*conditions).values(**values).returning(PrintQueueItem.id))
    ).all()
    if changed and commit:
        await db.commit()
    return bool(changed)


async def set_status_for_batch(
    db: AsyncSession,
    batch_id: str,
    new_status: str,
    *,
    queue_id: int | None = None,
    completed_at=None,
    commit: bool = True,
) -> int:
    """Move the batch's PENDING items of one printer queue to ``new_status``.  Returns count.

    One queue: ``queue_id``, or the batch's only queue (:func:`batch_queue_id` raises
    :class:`BatchSpansQueues` when its active rows sit in several, G03 D1). A compare-and-set
    on ``status='pending'``, like :func:`set_status`: a row claimed since the caller looked is
    left alone, never written over.
    """
    queue_id = await batch_queue_id(db, batch_id, queue_id)
    if queue_id is None or new_status == "pending":
        return 0
    values: dict = {"status": new_status, "deferred_claim_token": None}  # voids the deferral proof (A16)
    if completed_at is not None:
        values["completed_at"] = completed_at
    changed = (
        await db.execute(
            update(PrintQueueItem)
            .where(
                PrintQueueItem.batch_id == batch_id,
                PrintQueueItem.queue_id == queue_id,
                PrintQueueItem.status == "pending",
            )
            .values(**values)
            .returning(PrintQueueItem.id)
        )
    ).all()
    if changed and commit:
        await db.commit()
    return len(changed)
