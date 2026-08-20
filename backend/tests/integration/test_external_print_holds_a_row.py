"""A print BamDude did not send still occupies the printer, so it holds a row.

Not a bug fix — an external print already claims the queue at ``on_print_start``
and there is no dispatch window to protect, because we never dispatched. It is
consistency: after this, "which item is this printer running" has one answer for
all three ways a print can start, and the reprint work that follows can rely on
it.

⚠️ The discriminator is absence, not a new column. A print BamDude dispatched
arrives here with a row already — the scheduler's, or the claim a direct print
took for itself — so "no printing item on this queue" is what external means.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.app.main import mark_queue_printing_for_printer
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue


@pytest.fixture
def main_db(monkeypatch, db_session):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.main.async_session", _session_ctx)


async def _queue(db_session, printer_factory):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()
    return printer, queue


async def _printing_rows(db_session, queue_id):
    return (
        (
            await db_session.execute(
                select(PrintQueueItem)
                .where(PrintQueueItem.queue_id == queue_id)
                .where(PrintQueueItem.status == "printing")
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_external_print_gets_a_row(db_session, printer_factory, main_db):
    printer, queue = await _queue(db_session, printer_factory)

    await mark_queue_printing_for_printer(printer.id)

    rows = await _printing_rows(db_session, queue.id)
    assert len(rows) == 1
    await db_session.refresh(queue)
    assert queue.status == "printing"
    assert queue.current_item_id == rows[0].id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_row_carries_the_archive_so_completion_can_close_it(
    db_session, printer_factory, main_db, archive_factory
):
    """``on_print_complete`` auto-deletes a completed item only when it has an
    archive — without this the external row would outlive its print."""
    printer, queue = await _queue(db_session, printer_factory)
    archive = await archive_factory(printer.id, status="printing")

    await mark_queue_printing_for_printer(printer.id, archive_id=archive.id)

    rows = await _printing_rows(db_session, queue.id)
    assert len(rows) == 1
    assert rows[0].archive_id == archive.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_direct_prints_own_row_is_adopted_not_duplicated(db_session, printer_factory, main_db):
    """⚠️ ``on_print_start`` runs for our own dispatches too, and a second row
    there would trip on_print_complete's "Multiple queue items in 'printing'
    status" guard and attribute the print to whichever came back first."""
    from backend.app.services.queue_batch import claim_printer_for_direct_print

    printer, queue = await _queue(db_session, printer_factory)
    mine = await claim_printer_for_direct_print(db_session, printer_id=printer.id)

    await mark_queue_printing_for_printer(printer.id)

    rows = await _printing_rows(db_session, queue.id)
    assert [r.id for r in rows] == [mine.id]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_adopted_row_learns_its_archive(db_session, printer_factory, main_db, archive_factory):
    """A direct print's row is created before its archive exists — the dispatcher
    wires it, but a re-trigger path that adopts a different archive would leave
    the row pointing nowhere, and a completed item with no archive is never
    auto-deleted."""
    from backend.app.services.queue_batch import claim_printer_for_direct_print

    printer, queue = await _queue(db_session, printer_factory)
    mine = await claim_printer_for_direct_print(db_session, printer_id=printer.id)
    archive = await archive_factory(printer.id, status="printing")

    await mark_queue_printing_for_printer(printer.id, archive_id=archive.id)

    await db_session.refresh(mine)
    assert mine.archive_id == archive.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_scheduler_item_is_adopted_not_duplicated(db_session, printer_factory, main_db):
    printer, queue = await _queue(db_session, printer_factory)
    item = PrintQueueItem(queue_id=queue.id, status="printing", position=1, started_at=datetime.now(timezone.utc))
    db_session.add(item)
    await db_session.commit()

    await mark_queue_printing_for_printer(printer.id, item.id)

    rows = await _printing_rows(db_session, queue.id)
    assert len(rows) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_with_no_queue_row_is_a_noop(db_session, printer_factory, main_db):
    printer = await printer_factory()

    await mark_queue_printing_for_printer(printer.id)  # no error

    assert (await db_session.execute(select(PrintQueueItem))).scalars().all() == []
