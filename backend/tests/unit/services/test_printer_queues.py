"""Every printer has a queue, and the queue's id IS the printer's id.

The row used to be created in exactly one place — the HTTP add-printer route —
so a printer added from the Telegram bot, restored from elsewhere, or simply
older than m002's seed had none. Everything downstream then shrugged: the claim
a print takes at start returned None, the completion found no row to close, and
the plate gate armed anyway — so the card offered "Repeat" for a print that had
no row to re-arm, and the route answered 409 (2026-09-04). A missing queue is
created wherever it is first needed, and once at startup for everything else.

``id == printer_id`` is the invariant and not a preference: when the id is
occupied the guard repairs the occupant — adopting an orphan of a deleted
printer, sending another live printer's queue home — instead of creating the
new queue under the next free id, which is what made the equality a hope.
"""

import pytest
from sqlalchemy import delete as sa_delete, select

# ⚠️ Side effect, not the name: Printer declares its PrinterLocation relationship
# by string and SQLAlchemy cannot resolve it unless this module has been imported.
import backend.app.models.printer_location  # noqa: F401
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.printer_queues import ensure_all_printer_queues, ensure_printer_queue

pytestmark = pytest.mark.integration


async def test_a_missing_queue_is_created_under_the_printers_own_id(db_session, printer_factory):
    printer = await printer_factory()

    queue = await ensure_printer_queue(db_session, printer.id)
    await db_session.commit()

    assert queue.printer_id == printer.id
    assert queue.id == printer.id
    assert queue.status == "idle"


async def test_an_existing_queue_is_returned_not_duplicated(db_session, printer_factory):
    printer = await printer_factory()
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    await db_session.commit()

    first = await ensure_printer_queue(db_session, printer.id)
    second = await ensure_printer_queue(db_session, printer.id)

    assert first.id == second.id == printer.id
    rows = (await db_session.execute(select(PrinterQueue).where(PrinterQueue.printer_id == printer.id))).scalars().all()
    assert len(rows) == 1


async def test_an_orphan_on_the_printers_id_is_adopted(db_session, printer_factory):
    """⚠️ SQLite here never runs with ``PRAGMA foreign_keys = ON``, so a deleted
    printer's queue row outlives it and sits on the id a new printer later gets.
    The row belongs to the printer whose id it carries: the dead owner's items
    go, the row is reset, and the new printer takes it over."""
    orphan_owner = await printer_factory()
    printer = await printer_factory()
    # An orphan queue on ``printer.id`` still claiming to belong to somebody else.
    db_session.add(PrinterQueue(id=printer.id, printer_id=orphan_owner.id, status="printing", pending_count=3))
    await db_session.commit()
    db_session.add(PrintQueueItem(queue_id=printer.id, status="pending", position=0))
    await db_session.commit()
    # The printer goes. ⚠️ Not ``db_session.delete(orphan_owner)`` — ``Printer.queue``
    # carries ``cascade="all, delete-orphan"``, so the ORM takes the queue with it and
    # there is no orphan left to test. The row survives its owner exactly when the
    # printer leaves by some path that never loaded the relationship (raw SQL, a
    # restore, a migration) and SQLite does not enforce the FK.
    await db_session.execute(sa_delete(Printer).where(Printer.id == orphan_owner.id))
    await db_session.commit()

    queue = await ensure_printer_queue(db_session, printer.id)
    await db_session.commit()

    assert queue.id == printer.id
    assert queue.printer_id == printer.id
    assert queue.status == "idle"
    assert queue.pending_count == 0
    assert queue.current_item_id is None
    items = (await db_session.execute(select(PrintQueueItem))).scalars().all()
    assert items == []
    rows = (await db_session.execute(select(PrinterQueue))).scalars().all()
    assert [(q.id, q.printer_id) for q in rows] == [(printer.id, printer.id)]


async def test_a_squatter_made_queue_is_moved_to_the_printers_id(db_session, printer_factory):
    """A queue the old next-free-id branch created for a live printer moves home,
    its items and archives following the id."""
    printer = await printer_factory()
    wrong_id = printer.id + 100
    db_session.add(PrinterQueue(id=wrong_id, printer_id=printer.id))
    await db_session.commit()
    item = PrintQueueItem(queue_id=wrong_id, status="pending", position=0)
    archive = PrintArchive(
        printer_id=printer.id,
        queue_id=wrong_id,
        filename="w.3mf",
        file_path="",
        file_size=0,
        status="completed",
    )
    db_session.add_all([item, archive])
    await db_session.commit()

    queue = await ensure_printer_queue(db_session, printer.id)
    await db_session.commit()

    assert queue.id == printer.id
    assert queue.printer_id == printer.id
    assert await db_session.get(PrinterQueue, wrong_id) is None
    rows = (await db_session.execute(select(PrinterQueue))).scalars().all()
    assert len(rows) == 1
    moved_item = (await db_session.execute(select(PrintQueueItem))).scalars().one()
    assert moved_item.queue_id == printer.id
    moved_archive = (await db_session.execute(select(PrintArchive))).scalars().one()
    assert moved_archive.queue_id == printer.id


async def test_a_live_printers_queue_sitting_on_another_id_moves_home(db_session, printer_factory):
    """B's queue parked on A's id is not A's queue and is not deleted either —
    it goes to id B, and A gets the row that carries A's id."""
    printer_a = await printer_factory()
    printer_b = await printer_factory()
    db_session.add(PrinterQueue(id=printer_a.id, printer_id=printer_b.id))
    await db_session.commit()

    queue = await ensure_printer_queue(db_session, printer_a.id)
    await db_session.commit()

    assert (queue.id, queue.printer_id) == (printer_a.id, printer_a.id)
    rows = (await db_session.execute(select(PrinterQueue).order_by(PrinterQueue.id))).scalars().all()
    assert [(q.id, q.printer_id) for q in rows] == [
        (printer_a.id, printer_a.id),
        (printer_b.id, printer_b.id),
    ]


async def test_startup_fills_every_gap_once(db_session, printer_factory):
    with_queue = await printer_factory()
    without_a = await printer_factory()
    without_b = await printer_factory()
    db_session.add(PrinterQueue(id=with_queue.id, printer_id=with_queue.id))
    await db_session.commit()

    created = await ensure_all_printer_queues(db_session)
    assert created == 2

    ids = {q.printer_id: q.id for q in (await db_session.execute(select(PrinterQueue))).scalars().all()}
    assert ids == {with_queue.id: with_queue.id, without_a.id: without_a.id, without_b.id: without_b.id}

    assert await ensure_all_printer_queues(db_session) == 0


async def test_startup_repairs_a_misplaced_queue(db_session, printer_factory):
    """A row on the wrong id is breakage too — the sweep walks every printer, not
    only the ones with no queue at all, or it would walk straight past this one."""
    healthy = await printer_factory()
    misplaced = await printer_factory()
    db_session.add_all(
        [
            PrinterQueue(id=healthy.id, printer_id=healthy.id),
            PrinterQueue(id=misplaced.id + 100, printer_id=misplaced.id),
        ]
    )
    await db_session.commit()

    assert await ensure_all_printer_queues(db_session) == 1
    assert await ensure_all_printer_queues(db_session) == 0

    rows = (await db_session.execute(select(PrinterQueue))).scalars().all()
    assert {(q.id, q.printer_id) for q in rows} == {
        (healthy.id, healthy.id),
        (misplaced.id, misplaced.id),
    }
