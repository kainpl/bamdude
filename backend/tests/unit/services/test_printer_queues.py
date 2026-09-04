"""Every printer has a queue, and the queue's id is the printer's id.

The row used to be created in exactly one place — the HTTP add-printer route —
so a printer added from the Telegram bot, restored from elsewhere, or simply
older than m002's seed had none. Everything downstream then shrugged: the claim
a print takes at start returned None, the completion found no row to close, and
the plate gate armed anyway — so the card offered "Repeat" for a print that had
no row to re-arm, and the route answered 409 (2026-09-04). A missing queue is
created wherever it is first needed, and once at startup for everything else.
"""

import pytest
from sqlalchemy import select

# ⚠️ Side effect, not the name: Printer declares its PrinterLocation relationship
# by string and SQLAlchemy cannot resolve it unless this module has been imported.
import backend.app.models.printer_location  # noqa: F401
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


async def test_an_id_already_taken_falls_back_to_the_next_free_one(db_session, printer_factory):
    """⚠️ SQLite here never runs with ``PRAGMA foreign_keys = ON``, so a deleted
    printer's queue row can outlive it and sit on the id a new printer later
    gets. The preference is the printer's id; the guarantee is only that a
    queue exists — and that nothing assumes the two are equal (the completion
    handler looks the queue up by ``printer_id``)."""
    orphan_owner = await printer_factory()
    printer = await printer_factory()
    # An orphan queue squatting on ``printer.id`` but belonging to somebody else.
    db_session.add(PrinterQueue(id=printer.id, printer_id=orphan_owner.id))
    await db_session.commit()

    queue = await ensure_printer_queue(db_session, printer.id)
    await db_session.commit()

    assert queue.printer_id == printer.id
    assert queue.id != printer.id


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
