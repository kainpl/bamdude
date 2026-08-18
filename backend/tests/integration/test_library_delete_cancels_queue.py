"""A queue row must not outlive the file it would print.

Ported from upstream `02616f0c` (#2819), where the same fault wore two faces:
rows left pointing at a file that no longer existed — failing at the printer
with "Library file not found" days later — or rows deleted outright, with no
error and no history.

Both faces were here too, split across our two branches: a **managed** file is
soft-deleted and its queue rows were left pointing at a trashed file, while an
**external** file is hard-deleted and its rows were removed silently.

The row is now cancelled with a reason, which is what we already do when an
archive is trashed (``archive_purge._cancel_pending_queue_items``). A job that
vanished without a word is indistinguishable from one that was never queued.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.library_trash import library_trash_service


async def _file(db, *, external: bool = False) -> LibraryFile:
    row = LibraryFile(
        filename="part.gcode.3mf",
        file_path="files/part.gcode.3mf",
        file_type="3mf",
        file_size=10,
        is_external=external,
    )
    db.add(row)
    await db.flush()
    return row


async def _queued(db, file_id: int, printer_id: int, *, status: str = "pending") -> PrintQueueItem:
    queue = PrinterQueue(printer_id=printer_id, status="idle")
    db.add(queue)
    await db.flush()
    item = PrintQueueItem(queue_id=queue.id, library_file_id=file_id, position=1, status=status)
    db.add(item)
    await db.flush()
    return item


@pytest.mark.asyncio
@pytest.mark.integration
async def test_trashing_a_file_cancels_the_jobs_queued_against_it(db_session, printer_factory):
    printer = await printer_factory(name="P1")
    file = await _file(db_session)
    item = await _queued(db_session, file.id, printer.id)
    await db_session.commit()

    await library_trash_service.trash_or_purge(db_session, file)
    await db_session.commit()

    await db_session.refresh(item)
    assert item.status == "cancelled"
    assert item.waiting_reason == "Source file deleted"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_row_survives_so_the_operator_can_see_what_happened(db_session, printer_factory):
    """Cancelled, not deleted. A job that disappeared without a word looks like
    one that was never queued."""
    printer = await printer_factory(name="P2")
    file = await _file(db_session)
    item = await _queued(db_session, file.id, printer.id)
    item_id = item.id
    await db_session.commit()

    await library_trash_service.trash_or_purge(db_session, file)
    await db_session.commit()

    db_session.expire_all()
    still_there = await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))
    assert still_there.scalar_one_or_none() is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printing_row_is_left_alone(db_session, printer_factory):
    """Mid-print is the printer's race to lose, and its fail path catches it.
    Cancelling underneath a running job would be the worse answer."""
    printer = await printer_factory(name="P3")
    file = await _file(db_session)
    item = await _queued(db_session, file.id, printer.id, status="printing")
    await db_session.commit()

    await library_trash_service.trash_or_purge(db_session, file)
    await db_session.commit()

    await db_session.refresh(item)
    assert item.status == "printing"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_history_is_not_rewritten(db_session, printer_factory):
    """A completed row records what actually happened and must not become
    'cancelled' because somebody later tidied the library."""
    printer = await printer_factory(name="P4")
    file = await _file(db_session)
    item = await _queued(db_session, file.id, printer.id, status="completed")
    await db_session.commit()

    await library_trash_service.trash_or_purge(db_session, file)
    await db_session.commit()

    await db_session.refresh(item)
    assert item.status == "completed"
    assert item.waiting_reason is None
