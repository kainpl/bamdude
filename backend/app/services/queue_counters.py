"""Queue counter management — keeps PrinterQueue's live-state counters in sync.

Post-m019, terminal counters (``completed`` / ``failed`` / ``cancelled`` /
``total``) live in ``print_archives`` via ``archive.queue_id`` and are
computed on the read path by :func:`get_queue_terminal_counts` below.
Only ``pending_count`` and ``skipped_count`` stay cached on
``PrinterQueue`` — they track live queue items that aren't auto-cleaned.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue

logger = logging.getLogger(__name__)


async def update_queue_counters(db: AsyncSession, queue_id: int) -> None:
    """Recount the live-state counters (pending + skipped) for a queue.

    Terminal counters moved to :func:`get_queue_terminal_counts` (archive-backed).
    """
    result = await db.execute(
        select(PrinterQueue).where(PrinterQueue.id == queue_id).execution_options(populate_existing=True)
    )
    queue = result.scalar_one_or_none()
    if not queue:
        return

    counts = {}
    for status in ("pending", "skipped"):
        result = await db.execute(
            select(func.count())
            .select_from(PrintQueueItem)
            .where(PrintQueueItem.queue_id == queue_id, PrintQueueItem.status == status)
        )
        counts[status] = result.scalar() or 0

    queue.pending_count = counts["pending"]
    queue.skipped_count = counts["skipped"]
    queue.last_activity_at = datetime.now(timezone.utc)


async def get_queue_terminal_counts(db: AsyncSession, queue_id: int) -> dict[str, int]:
    """Count terminal-status archives for a queue.

    Read path for ``GET /printer-queues/`` — replaces the removed cached
    columns (``completed_count``/``failed_count``/``cancelled_count``/
    ``total_count``). Treats the whole failure family (``failed`` /
    ``aborted`` / ``cancelled`` / ``stopped``) as "cancelled" for the
    legacy display split, with a dedicated ``failed`` breakdown as well.
    """
    result = await db.execute(
        select(PrintArchive.status, func.count())
        .where(func.coalesce(PrintArchive.extra_data["dispatch_aborted"].as_boolean(), False).is_(False))
        .where(PrintArchive.queue_id == queue_id)
        .group_by(PrintArchive.status)
    )
    by_status = {row[0]: int(row[1] or 0) for row in result.all()}

    completed = by_status.get("completed", 0)
    failed = by_status.get("failed", 0)
    cancelled = by_status.get("cancelled", 0) + by_status.get("aborted", 0) + by_status.get("stopped", 0)
    total = sum(by_status.values())

    return {
        "completed_count": completed,
        "failed_count": failed,
        "cancelled_count": cancelled,
        "total_count": total,
    }


async def set_queue_printing(db: AsyncSession, queue_id: int, item_id: int | None = None) -> None:
    """Set queue to printing status when a print starts.

    ``item_id`` may be None for prints that have no corresponding
    ``PrintQueueItem`` row (external prints, BamDude direct dispatch).
    In that case the queue still transitions to "printing" so the UI
    reflects the real printer state — the queue widget then synthesises
    a virtual current-print item for display.

    **Do not** clobber ``current_item_id`` with None when the scheduler
    already set it to a valid queue-item id earlier in the dispatch
    flow. Previously ``on_print_start`` paths called this with no
    ``item_id`` and wiped the value; the UI then lost track of which
    queued item was live. If a caller explicitly wants to clear the
    pointer (terminal transition), use :func:`set_queue_idle` instead.
    """
    result = await db.execute(
        select(PrinterQueue).where(PrinterQueue.id == queue_id).execution_options(populate_existing=True)
    )
    queue = result.scalar_one_or_none()
    if not queue:
        return

    queue.status = "printing"
    if item_id is not None:
        queue.current_item_id = item_id
    queue.last_activity_at = datetime.now(timezone.utc)


async def release_lost_claim(db: AsyncSession, queue_id: int, item_id: int) -> bool:
    """A lost compare-and-set leaves no claimed header (G03 B, A11).

    An attempt end whose CAS did not own its row (the row was cancelled, deleted or claimed again) releases the
    claim queue's header only while it still names this item as ``printing`` and the row is not ``printing``: a
    row claimed again is ``printing``, so a later attempt's header is never released. Inside the lane.
    """
    still_printing = (
        select(PrintQueueItem.id).where(PrintQueueItem.id == item_id, PrintQueueItem.status == "printing").exists()
    )
    result = await db.execute(
        update(PrinterQueue)
        .where(
            PrinterQueue.id == queue_id,
            PrinterQueue.current_item_id == item_id,
            PrinterQueue.status == "printing",
            ~still_printing,
        )
        .values(status="idle", current_item_id=None, last_activity_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount:
        logger.info("Queue %d released: its claim on item %s was lost", queue_id, item_id)
    return bool(result.rowcount)


HaltKind = str  # "error" | "paused"


async def halt_lane(
    db: AsyncSession,
    queue_id: int,
    *,
    item_id: int | None,
    kind: HaltKind,
    stale_only: bool = False,
    read_item_id: int | None = None,
) -> str:
    """Halt a queue until Resume without ever overwriting another run's claim (G03 B, A9).

    The one writer of a halt: every verdict, attempt end and terminal close that halts a queue calls it inside its
    lane, which already holds the header's lock. Plain form (``stale_only=False``):

    * the header names ``item_id`` (an attempt end), or the lane has no live claim at all (a refusal before the
      claim) → ``status = kind`` (``error`` / ``paused``) and ``current_item_id = NULL`` -- unless an earlier halt
      already holds the lane and the header no longer names the row (``unchanged``: that halt stands);
    * otherwise another run holds the printer (the header is ``printing`` for another row, or another row is
      ``printing``) → ``is_paused = True`` only: the next row waits for Resume, the other run's ``status`` and
      ``current_item_id`` are left as they are, and its own end does not clear the pause.

    Addressed form (``stale_only=True``) for a header no claim owns: the compare-and-set ``status = 'printing' AND
    current_item_id IS NOT DISTINCT FROM read_item_id AND no printing row``; when a printing row exists (a live
    owner) it pauses only, and when the header has moved on since ``read_item_id`` was read it writes nothing.

    Returns what it did: ``halted``, ``paused_only``, ``unchanged`` or ``missing``.
    """
    if kind not in {"error", "paused"}:
        raise ValueError(f"halt kind {kind!r}")
    now = datetime.now(timezone.utc)
    printing_row = (
        select(PrintQueueItem.id)
        .where(PrintQueueItem.queue_id == queue_id, PrintQueueItem.status == "printing")
        .limit(1)
    )
    if stale_only:
        result = await db.execute(
            update(PrinterQueue)
            .where(
                PrinterQueue.id == queue_id,
                PrinterQueue.status == "printing",
                PrinterQueue.current_item_id.is_not_distinct_from(read_item_id),
                ~printing_row.exists(),
            )
            .values(status=kind, current_item_id=None, last_activity_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount:
            logger.info("Queue %d halted (%s): its stale header was owned by no claim", queue_id, kind)
            return "halted"
        owner = await db.scalar(printing_row)
        header = await db.scalar(
            select(PrinterQueue.id).where(
                PrinterQueue.id == queue_id,
                PrinterQueue.status == "printing",
                PrinterQueue.current_item_id.is_not_distinct_from(read_item_id),
            )
        )
        if owner is not None and header is not None:
            await db.execute(
                update(PrinterQueue)
                .where(PrinterQueue.id == queue_id)
                .values(is_paused=True, last_activity_at=now)
                .execution_options(synchronize_session=False)
            )
            logger.info("Queue %d paused: row %s of another run is printing (%s not applied)", queue_id, owner, kind)
            return "paused_only"
        return "unchanged"

    queue = await db.scalar(
        select(PrinterQueue)
        .where(PrinterQueue.id == queue_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if queue is None:
        return "missing"
    names_this_item = item_id is not None and queue.current_item_id == item_id
    live_claim = queue.status == "printing" or (await db.scalar(printing_row)) is not None
    if not names_this_item and not live_claim and queue.status in {"error", "paused"}:
        # Already held until Resume by an earlier halt (a swap hold, a cancel): that halt and its reason stand.
        logger.info("Queue %d already halted (%s); %s for item %s not applied", queue_id, queue.status, kind, item_id)
        return "unchanged"
    if names_this_item or not live_claim:
        queue.status = kind
        queue.current_item_id = None
        queue.last_activity_at = now
        logger.info("Queue %d halted (%s) for item %s", queue_id, kind, item_id)
        return "halted"
    queue.is_paused = True
    queue.last_activity_at = now
    logger.info(
        "Queue %d paused for item %s: another run holds it (current item %s, %s not applied)",
        queue_id,
        item_id,
        queue.current_item_id,
        kind,
    )
    return "paused_only"


async def set_queue_idle(
    db: AsyncSession,
    queue_id: int,
    *,
    expected_item_id: int | None = None,
    stale_only: bool = False,
    read_item_id: int | None = None,
) -> bool:
    """Release a queue only while its persisted state is ``printing``.

    Completion handlers can run after FTP, MQTT, or macro awaits.  Their
    ``AsyncSession`` may still hold an old ``PrinterQueue`` object, so an ORM
    read/compare here could turn a concurrent ``paused`` or ``error`` state
    back into ``idle``.  Keep the condition in the database instead.

    ``stale_only`` (G03 B, A18 / A9): release only a header no claim owns -- the compare-and-set on the header as
    read (``current_item_id IS NOT DISTINCT FROM read_item_id``) with no ``printing`` row of the queue. Returns
    whether the header was released.
    """
    conditions = [PrinterQueue.id == queue_id, PrinterQueue.status == "printing"]
    if expected_item_id is not None:
        conditions.append(PrinterQueue.current_item_id == expected_item_id)
    if stale_only:
        conditions.append(PrinterQueue.current_item_id.is_not_distinct_from(read_item_id))
        conditions.append(
            ~select(PrintQueueItem.id)
            .where(PrintQueueItem.queue_id == queue_id, PrintQueueItem.status == "printing")
            .exists()
        )
    statement = (
        update(PrinterQueue)
        .where(*conditions)
        .values(
            status="idle",
            current_item_id=None,
            last_activity_at=datetime.now(timezone.utc),
        )
    )
    if stale_only:  # a subquery term the session cannot evaluate against its objects
        statement = statement.execution_options(synchronize_session=False)
    result = await db.execute(statement)
    return bool(result.rowcount)


class AutoRowOrderError(RuntimeError):
    """A programmer's error against the AutoQueue lock order (G03 B, A20): a transaction asked for a router row with
    an id lower than one it already holds. Raised in production too, as ``product_gate`` raises ``GateOrderError``
    -- a lower id after a higher one is how two writers of overlapping sets deadlock."""


async def lock_auto_rows(db: AsyncSession, ids) -> dict:
    """Lock router rows ascending by id, ``FOR NO KEY UPDATE``, and re-read them (``populate_existing``) (G03 B, A20).

    Every transaction that writes or deletes more than one AutoQueue row calls this before its first write, with the
    complete set it will write: the ids it locks are recorded in the transaction's lock ledger (``core/lock_ledger``,
    which follows savepoints), an id it already holds is not asked for again -- a nested helper re-asking for rows of
    the same set is a no-op -- and an id lower than one already held raises :class:`AutoRowOrderError`. A row this
    transaction inserted waits for nobody and is not ordered. SQLite: the caller already holds the writer; the read
    is the re-read. Returns ``{id: row}`` for the rows that exist.
    """
    from backend.app.core.lock_ledger import ledger
    from backend.app.models.auto_queue import AutoQueueItem

    wanted = sorted({int(i) for i in ids})
    if not wanted:
        return {}
    held = ledger(db)
    table = AutoQueueItem.__table__.name
    existing = held.held_existing(table)
    new = [i for i in wanted if not held.holds(table, i) and (table, i) not in held.created_rows]
    if new and existing and new[0] < max(existing):
        raise AutoRowOrderError(
            f"auto_queue_items #{new[0]} locked after #{max(existing)}: one complete set per transaction, ascending"
        )
    statement = select(AutoQueueItem).where(AutoQueueItem.id.in_(wanted)).order_by(AutoQueueItem.id)
    if db.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update(key_share=True)
    rows = {row.id: row for row in await db.scalars(statement.execution_options(populate_existing=True))}
    held.locked_rows.update((table, i) for i in new)
    return rows


async def companion_auto_ids(db: AsyncSession, item_ids: list[int]) -> list[int]:
    """Only the router incarnation stamped on this dispatched row is its companion."""
    from backend.app.models.auto_queue import AutoQueueItem

    return list(
        await db.scalars(
            select(AutoQueueItem.id)
            .join(
                PrintQueueItem,
                (PrintQueueItem.source_auto_item_id == AutoQueueItem.id)
                & (PrintQueueItem.source_auto_identity == AutoQueueItem.recovery_identity),
            )
            .where(PrintQueueItem.id.in_(item_ids), PrintQueueItem.source_auto_identity.is_not(None))
        )
    )


async def detach_print_queue_refs(db: AsyncSession, item_ids: list[int], *, outside_lanes: str | None = None) -> None:
    """Clean up rows referencing ``print_queue`` items about to be deleted.

    SQLite runs with ``foreign_keys=OFF`` so the ``ON DELETE`` clauses on
    these FKs never fire — do the cleanup explicitly (also keeps the
    behaviour identical on PostgreSQL). Call this immediately before
    deleting the ``PrintQueueItem`` row(s).

      * ``auto_queue_items`` — the pre-dispatch *router* row. Once its
        dispatched ``print_queue`` item is gone it carries nothing, so
        delete it. The archive keeps ``from_auto_queue=True`` as the
        lasting record (auto-queue stats count that, not this row).
      * ``calibration_session`` — calibration *history*; keep the row,
        just null its ``print_queue_item_id`` link.
    """
    if not item_ids:
        return

    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.models.calibration_session import CalibrationSession

    auto_ids = await companion_auto_ids(db, item_ids)
    backrefs = list(await db.scalars(select(AutoQueueItem.id).where(AutoQueueItem.assigned_to_item_id.in_(item_ids))))
    # Every router row this detach writes -- the companions it deletes, the back-references it nulls -- locked
    # ascending before the first write (G03 B, A20); a caller that already holds them asks for nothing again.
    await lock_auto_rows(db, [*auto_ids, *backrefs])
    if auto_ids:
        await detach_auto_queue_refs(db, auto_ids, outside_lanes=outside_lanes)
        await db.execute(delete(AutoQueueItem).where(AutoQueueItem.id.in_(auto_ids)))
    # Other or unproven router rows retain their work/bytes. Only their
    # navigation to the removed printer row is cut (SQLite runs no SET NULL).
    await db.execute(
        update(AutoQueueItem).where(AutoQueueItem.assigned_to_item_id.in_(item_ids)).values(assigned_to_item_id=None)
    )

    await db.execute(
        update(CalibrationSession)
        .where(CalibrationSession.print_queue_item_id.in_(item_ids))
        .values(print_queue_item_id=None)
    )


async def detach_auto_queue_refs(db: AsyncSession, item_ids: list[int], *, outside_lanes: str | None = None) -> None:
    """Cut only mutable navigation; the dispatched incarnation remains history.

    A printer row this nulls lies in a lane the caller holds (G03 B, A20): one outside them -- a placement that
    committed after the caller read its footprint -- is the caller's refusal (409, nothing written), never a row
    written outside its lane. ``outside_lanes`` names why a caller holds none (start-up, before any lane holder).
    """
    if item_ids:
        if outside_lanes is None:
            from backend.app.services.queue_ops import held_lanes

            lanes = set(
                await db.scalars(
                    select(PrintQueueItem.queue_id).where(PrintQueueItem.source_auto_item_id.in_(item_ids))
                )
            )
            if not lanes <= held_lanes(db):
                from backend.app.services.queue_cancel_scope import selection_changed

                raise selection_changed()
        await db.execute(
            update(PrintQueueItem)
            .where(PrintQueueItem.source_auto_item_id.in_(item_ids))
            .values(source_auto_item_id=None)
        )
