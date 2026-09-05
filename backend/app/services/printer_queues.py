"""Every printer has a queue row, and the queue's id IS the printer's id.

``printer_queues`` used to be created in exactly one place — the HTTP
add-printer route — and assumed everywhere else. A printer added from the
Telegram bot, restored from a backup, or older than m002's seed had none, and
nothing downstream complained: the claim a print takes at start returned None,
the completion then found no row to close, and the plate gate armed regardless
— so the card offered "Repeat" for a print with no row to re-arm and the route
answered ``409 No finished print is waiting`` (reported 2026-09-04, a print
started from the printer's own screen).

Two guards, both idempotent: ``ensure_printer_queue`` wherever a queue is
first needed, and ``ensure_all_printer_queues`` once at startup.

``PrinterQueue.id == PrinterQueue.printer_id`` is the INVARIANT, not a
preference: the row carrying a printer's id is that printer's queue, and
nothing here may create a ``PrinterQueue`` under any other id. The scheduler
reads ``item.queue_id`` as the printer id and is entitled to.

SQLite here never runs with ``PRAGMA foreign_keys = ON``, so a deleted
printer's queue can outlive it and sit on the id a new printer later receives,
and an older build of this module answered that by creating the new queue under
the next free id — which made the invariant a hope. The guard repairs instead
of yielding:

* the row on the printer's id owned by a printer that **exists** is that
  printer's queue parked on the wrong id — it is moved home first
  (``_rekey_queue``), which frees the id;
* a row that carries the printer's ``printer_id`` under some other id is moved
  onto the printer's id, its queue items and archives following it;
* the row on the printer's id owned by a printer that is **gone** is an orphan
  — its stale items are dropped, the archives that referenced it are detached,
  and the row is reset and adopted.

⚠️ Those last two are tried in that order, and the order is the whole point:
the removed next-free-id branch produced installs holding BOTH at once — an
orphan of a deleted printer on id X and printer X's real queue parked
elsewhere. Adopting first would write ``printer_id = X`` onto the orphan while
the real queue still carries it, and ``UNIQUE(printer_id)`` would reject it from
every caller. Re-keying the real queue first drops the orphan on the way past.

⚠️ A ring of misplaced rows (A's queue on B's id and B's on A's) cannot be
repaired at all: every move needs the other id free first. ``_rekey_queue``
raises ``RuntimeError`` on it rather than looping, and the startup sweep logs it
and carries on to the other printers.

⚠️ The re-key updates a primary key with children still pointing at the old
value, which only passes where the FK is not enforced statement-by-statement —
SQLite, which is where these rows arise (PostgreSQL cascades the printer delete
and never leaves an orphan behind). ``print_archives.queue_id`` is an FK as
well as ``print_queue.queue_id``, so on PostgreSQL the re-key is refused for any
queue that has ever recorded an archive — practically every queue on a live
farm, not merely one with items still on it. No reordering of the three
statements helps: a child needs a parent that exists, a parent's key cannot move
while a child references it, and ``UNIQUE NOT NULL printer_id`` forbids
standing a second row up beside it in the meantime. The fix is a migration
adding ``ON UPDATE CASCADE`` to those two FKs (preferred over making them
DEFERRABLE, which would buy the same thing only inside one transaction);
meanwhile the startup sweep logs the refusal per printer and main.py's guard
keeps it away from startup.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue

logger = logging.getLogger(__name__)


async def ensure_printer_queue(
    db: AsyncSession,
    printer_id: int,
    _visited: set[int] | None = None,
) -> PrinterQueue:
    """The printer's queue row — the row whose id IS ``printer_id``.

    Looks that row up first and repairs whatever it finds instead of stepping
    aside: another live printer's queue is moved home, a row already carrying
    ``printer_id`` under some other id is re-keyed onto it, and an orphan of a
    deleted printer is adopted. Only when the id is genuinely free is a queue
    created, and then always as ``PrinterQueue(id=printer_id, ...)``.

    ⚠️ Adoption is the LAST resort, not the first: an orphan on ``printer_id``
    and the printer's real queue parked elsewhere occur together, and adopting
    then writes a ``printer_id`` a live row already holds — ``UNIQUE`` rejects
    it. Re-keying the real queue home drops the orphan on the way past.

    Flushes but does not commit — the caller owns the transaction, so the row
    lands together with whatever needed it (an archive, a claim, the printer).

    ``_visited`` is internal: the repair paths recurse through ``_rekey_queue``
    when another printer's queue is in the way, and a ring of misplaced rows
    would otherwise loop for ever.
    """
    visited = _visited if _visited is not None else set()
    if printer_id in visited:
        raise RuntimeError(f"printer_queues form a re-keying cycle — printer {printer_id} was reached twice")
    visited.add(printer_id)

    orphan: PrinterQueue | None = None
    row = await db.get(PrinterQueue, printer_id)
    if row is not None:
        if row.printer_id == printer_id:
            return row
        if await db.get(Printer, row.printer_id) is None:
            # Held for the fall-through: the printer's real queue may still be
            # out there, and it has the better claim on the id.
            orphan = row
        else:
            # A live printer's queue parked on somebody else's id (the old
            # next-free-id branch made these). Moving it home frees ``printer_id``.
            await _rekey_queue(db, row, new_id=row.printer_id, visited=visited)

    misplaced = (
        await db.execute(select(PrinterQueue).where(PrinterQueue.printer_id == printer_id))
    ).scalar_one_or_none()
    if misplaced is not None:
        # ``_rekey_queue`` clears an orphan occupying the target on its own.
        return await _rekey_queue(db, misplaced, new_id=printer_id, visited=visited)
    if orphan is not None:
        return await _adopt_orphan(db, orphan, printer_id)

    queue = PrinterQueue(id=printer_id, printer_id=printer_id)
    db.add(queue)
    await db.flush()
    logger.info("Created queue %s for printer %s", queue.id, printer_id)
    return queue


async def ensure_all_printer_queues(db: AsyncSession) -> int:
    """Run the guard for EVERY printer, not only the ones missing a queue.

    A missing row is not the only breakage — a row sitting on the wrong id is
    one too, and only a pass over every printer finds it. Returns how many
    printers had their queue created or repaired; a healthy install returns 0
    and logs nothing.

    ⚠️ One printer that cannot be repaired must not take the rest of the farm
    with it — a ring of misplaced rows is unrepairable by design, and on
    PostgreSQL the re-key is refused outright — so each printer is guarded and
    only the ones that came back count. (A refusal that poisons the transaction,
    which is what PostgreSQL does with an IntegrityError, will still fail every
    printer after it; the next call retries the lot.)

    ⚠️ The count is taken from a snapshot read BEFORE anything moves, not per
    iteration: repairing one printer can put another printer's row right on the
    way past (its queue was parked on this one's id), and by its own turn that
    printer would look like it had been fine all along.
    """
    printer_ids = (await db.execute(select(Printer.id).order_by(Printer.id))).scalars().all()
    placed = (await db.execute(select(PrinterQueue.id, PrinterQueue.printer_id))).all()
    correct = {printer_id for queue_id, printer_id in placed if queue_id == printer_id}
    needed = [printer_id for printer_id in printer_ids if printer_id not in correct]

    failed: set[int] = set()
    for printer_id in printer_ids:
        try:
            await ensure_printer_queue(db, printer_id)
        except Exception:  # noqa: BLE001 — one unrepairable printer is not the others' problem
            logger.exception("Startup: could not repair the queue of printer %s", printer_id)
            failed.add(printer_id)
    repaired = [printer_id for printer_id in needed if printer_id not in failed]
    if repaired:
        await db.commit()
        logger.info("Startup: created or repaired the queue of %d printer(s): %s", len(repaired), repaired)
    return len(repaired)


async def _adopt_orphan(db: AsyncSession, row: PrinterQueue, printer_id: int) -> PrinterQueue:
    """Take over the row on ``printer_id``'s id whose owner no longer exists.

    Its items describe a machine that is gone, so they are dropped and the
    archives that pointed at the queue are detached; everything else is reset
    to what a freshly created queue would carry — a stale ``is_paused`` or
    ``auto_distribute_eligible=False`` inherited from the dead printer would
    silently stop the new one from ever being dispatched to.
    """
    previous_owner = row.printer_id
    dropped = await _detach_queue_children(db, row.id)
    row.printer_id = printer_id
    row.status = "idle"
    row.is_paused = False
    row.last_activity_at = None
    row.current_item_id = None
    row.pending_count = 0
    row.skipped_count = 0
    row.auto_distribute_eligible = True
    await db.flush()
    logger.warning(
        "Adopted orphan queue %s for printer %s (its previous owner %s is gone; %d stale item(s) dropped)",
        row.id,
        printer_id,
        previous_owner,
        dropped,
    )
    return row


async def _detach_queue_children(db: AsyncSession, queue_id: int) -> int:
    """Drop the queue's items and null the archives that reference it.

    Returns how many items were dropped. SQLite ignores the FK actions, so both
    halves are done here in code — the archive column is ``ON DELETE SET NULL``
    by declaration only.
    """
    dropped = (await db.execute(delete(PrintQueueItem).where(PrintQueueItem.queue_id == queue_id))).rowcount or 0
    await db.execute(update(PrintArchive).where(PrintArchive.queue_id == queue_id).values(queue_id=None))
    return dropped


async def _rekey_queue(
    db: AsyncSession,
    row: PrinterQueue,
    new_id: int,
    visited: set[int],
) -> PrinterQueue:
    """Move ``row`` onto ``new_id`` — its owner's id — taking its children with it.

    ``new_id`` must be free by the time the update runs: an orphan sitting on
    it is deleted, and a live printer's queue is sent home first by recursing
    into ``ensure_printer_queue`` (``visited`` turns a ring of misplaced rows
    into a ``RuntimeError`` instead of a loop).
    """
    old_id = row.id
    owner = row.printer_id
    if old_id == new_id:  # pragma: no cover — callers only re-key a row that is elsewhere
        return row

    occupant = await db.get(PrinterQueue, new_id)
    if occupant is not None:
        if await db.get(Printer, occupant.printer_id) is None:
            await _detach_queue_children(db, occupant.id)
            db.expunge(occupant)
            await db.execute(delete(PrinterQueue).where(PrinterQueue.id == new_id))
            await db.flush()
            logger.warning("Dropped orphan queue %s (its owner %s is gone)", new_id, occupant.printer_id)
        else:
            await ensure_printer_queue(db, occupant.printer_id, visited)
        still_taken = await db.get(PrinterQueue, new_id)
        if still_taken is not None:
            raise RuntimeError(
                f"cannot re-key queue {old_id} → {new_id} for printer {owner}: "
                f"the id is still held by printer {still_taken.printer_id}"
            )

    await db.execute(update(PrintQueueItem).where(PrintQueueItem.queue_id == old_id).values(queue_id=new_id))
    await db.execute(update(PrintArchive).where(PrintArchive.queue_id == old_id).values(queue_id=new_id))
    # ⚠️ The identity map keys on the primary key we are about to change: leave
    # the old instance attached and the next ``PrinterQueue(id=old_id, ...)``
    # collides with a ghost that no longer exists in the database.
    db.expunge(row)
    await db.execute(update(PrinterQueue).where(PrinterQueue.id == old_id).values(id=new_id))
    await db.flush()

    moved = await db.get(PrinterQueue, new_id)
    if moved is None:  # pragma: no cover — the update above just wrote this row
        raise RuntimeError(f"re-keyed queue {old_id} → {new_id} vanished")
    logger.warning(
        "Re-keyed queue %s → %s for printer %s (a queue's id is its printer's id)",
        old_id,
        new_id,
        owner,
    )
    return moved
