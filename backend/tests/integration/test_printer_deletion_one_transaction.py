"""Printer deletion's queue part is one transaction under the printer's lanes (G03 B, A21 r5 R20 / F3; TA26 c, d).

Step 2 takes the printer row, the headers ``FOR UPDATE``, the archive rows of the printer and of its queues, the
maintenance rows, the AutoQueue companions and every job row in one transaction, and commits once. The archives'
``printer_id`` / ``queue_id`` are nulled explicitly (the header's cascade would otherwise do it by an FK action after
the job rows). A failure anywhere in it leaves every one of those rows as it was. When the archives are deleted
with the printer (the default), each is deleted and committed on its own before step 2 (A21 step 1), so a failure in
step 2 keeps the ones already removed removed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from backend.app.core import database
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def a_printer_with_history(db_session, printer_factory, archive_factory):
    printer = await printer_factory(name="Retiring")
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="idle")
    db_session.add(queue)
    await db_session.flush()
    archive = await archive_factory(printer.id, queue_id=queue.id, status="completed")
    db_session.add_all(
        [
            PrintQueueItem(queue_id=queue.id, position=1, status="pending"),
            PrintQueueItem(queue_id=queue.id, position=2, status="completed", archive_id=archive.id),
        ]
    )
    await db_session.commit()
    return printer.id, queue.id, archive.id


async def counts(printer_id, queue_id, archive_id):
    async with database.async_session() as db:
        archive = await db.get(PrintArchive, archive_id)
        return (
            await db.get(Printer, printer_id) is not None,
            await db.get(PrinterQueue, queue_id) is not None,
            await db.scalar(
                select(func.count()).select_from(PrintQueueItem).where(PrintQueueItem.queue_id == queue_id)
            ),
            (archive.printer_id, archive.queue_id) if archive is not None else None,
        )


async def test_keeping_the_archives_detaches_them_explicitly(
    async_client, db_session, printer_factory, archive_factory
):
    printer_id, queue_id, archive_id = await a_printer_with_history(db_session, printer_factory, archive_factory)
    response = await async_client.delete(f"/api/v1/printers/{printer_id}?delete_archives=false")
    assert response.status_code == 200, response.text
    assert await counts(printer_id, queue_id, archive_id) == (False, False, 0, (None, None))


async def test_a_failure_inside_the_transaction_leaves_every_row_as_it_was(
    async_client, db_session, printer_factory, archive_factory, monkeypatch
):
    from backend.app.services import spoolman_usage_history

    printer_id, queue_id, archive_id = await a_printer_with_history(db_session, printer_factory, archive_factory)
    before = await counts(printer_id, queue_id, archive_id)

    async def fails_after_the_job_rows(db, printer_id):
        raise RuntimeError("injected after the job rows were deleted")

    monkeypatch.setattr(spoolman_usage_history, "detach_printer", fails_after_the_job_rows)
    with pytest.raises(RuntimeError, match="injected"):
        await async_client.delete(f"/api/v1/printers/{printer_id}?delete_archives=false")
    assert before == (True, True, 2, (printer_id, queue_id))
    assert await counts(printer_id, queue_id, archive_id) == before, "a partial step 2 committed"


async def test_by_default_the_archives_go_first_and_a_failure_in_the_final_step_keeps_the_rest(
    async_client, db_session, printer_factory, archive_factory, monkeypatch
):
    """The default (``delete_archives`` true): every archive is deleted on its own, committed, before the final
    step. A failure in that step leaves the printer, its queue and its jobs as they were; the archives it already
    removed stay removed -- deleting again finishes the job (review B1-05)."""
    from backend.app.services import spoolman_usage_history

    printer_id, queue_id, archive_id = await a_printer_with_history(db_session, printer_factory, archive_factory)
    real = spoolman_usage_history.detach_printer

    async def fails_once(db, printer_id):
        monkeypatch.setattr(spoolman_usage_history, "detach_printer", real)
        raise RuntimeError("injected after the job rows were deleted")

    monkeypatch.setattr(spoolman_usage_history, "detach_printer", fails_once)
    with pytest.raises(RuntimeError, match="injected"):
        await async_client.delete(f"/api/v1/printers/{printer_id}")
    assert await counts(printer_id, queue_id, archive_id) == (True, True, 2, None)

    response = await async_client.delete(f"/api/v1/printers/{printer_id}")
    assert response.status_code == 200, response.text
    assert await counts(printer_id, queue_id, archive_id) == (False, False, 0, None)
