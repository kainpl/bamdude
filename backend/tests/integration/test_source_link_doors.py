"""Every restore or copy of a job's source link names a proven incarnation or nothing (G03 B, A27; TA37 (b), (d),
(e) on SQLite for Retry, Unskip, bulk Retry, Clone and plate Repeat).

A job captured from archive A (its provenance stamped with A's incarnation) whose attempt re-pointed its link to
its execution archive X. SQLite hands a deleted archive's id to the next archive; a link written because the id
exists would name that other print.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import update

from backend.app.core import database
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import QueueSource

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

DONE = datetime(2026, 11, 20, 9, 0, 0)


async def a_finished_job(db_session, printer_factory, archive_factory, *, status="failed", stamped=True):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="idle")
    db_session.add(queue)
    original = await archive_factory(printer.id, status="completed")
    execution = await archive_factory(printer.id, status="failed")
    blob = QueueSource(sha256="d" * 64, size_bytes=1, relative_path="objects/dd/job.3mf", format="3mf", state="ready")
    db_session.add(blob)
    await db_session.flush()
    provenance = {"kind": "archive", "id": original.id}
    if stamped:
        provenance["incarnation"] = original.recovery_identity
    item = PrintQueueItem(
        queue_id=queue.id,
        queue_source_id=blob.id,
        source_snapshot={"version": 1, "provenance": provenance},
        archive_id=execution.id,
        status=status,
        position=0,
        started_at=DONE,
        completed_at=DONE,
    )
    db_session.add(item)
    await db_session.commit()
    return printer.id, queue.id, original.id, execution.id, item.id


async def replace_the_original(original_id: int) -> None:
    """SQLite: the original deleted and another archive inserted under its id."""
    async with database.async_session() as db:
        await db.execute(PrintArchive.__table__.delete().where(PrintArchive.id == original_id))
        db.add(PrintArchive(id=original_id, filename="other.3mf", file_path="", file_size=0, status="completed"))
        await db.commit()


async def link(item_id: int) -> tuple[int | None, int | None]:
    async with database.async_session() as db:
        row = await db.get(PrintQueueItem, item_id)
        return row.archive_id, row.library_file_id


@pytest.mark.parametrize("replaced", [False, True])
async def test_retry(async_client, db_session, printer_factory, archive_factory, replaced):
    _printer, _queue, original, _x, item_id = await a_finished_job(db_session, printer_factory, archive_factory)
    if replaced:
        await replace_the_original(original)
    response = await async_client.post(f"/api/v1/queue/{item_id}/retry")
    assert response.status_code == 200, response.text
    assert await link(item_id) == ((None, None) if replaced else (original, None))


@pytest.mark.parametrize("replaced", [False, True])
async def test_unskip(async_client, db_session, printer_factory, archive_factory, replaced):
    _printer, _queue, original, _x, item_id = await a_finished_job(
        db_session, printer_factory, archive_factory, status="skipped"
    )
    if replaced:
        await replace_the_original(original)
    response = await async_client.post(f"/api/v1/queue/{item_id}/unskip")
    assert response.status_code == 200, response.text
    assert await link(item_id) == ((None, None) if replaced else (original, None))


@pytest.mark.parametrize("replaced", [False, True])
async def test_bulk_retry(async_client, db_session, printer_factory, archive_factory, replaced):
    _printer, queue_id, original, _x, item_id = await a_finished_job(db_session, printer_factory, archive_factory)
    if replaced:
        await replace_the_original(original)
    response = await async_client.post("/api/v1/queue/bulk-retry", json={"queue_id": queue_id, "item_ids": [item_id]})
    assert response.status_code == 200, response.text
    assert await link(item_id) == ((None, None) if replaced else (original, None))


async def test_a_snapshot_without_a_stamp_keeps_no_link_to_a_re_pointed_job(
    async_client, db_session, printer_factory, archive_factory
):
    """TA37 (e): captured before the stamp, its link re-pointed to X — the job prints its own bytes, NULL."""
    _printer, _queue, _original, _x, item_id = await a_finished_job(
        db_session, printer_factory, archive_factory, stamped=False
    )
    response = await async_client.post(f"/api/v1/queue/{item_id}/retry")
    assert response.status_code == 200, response.text
    assert await link(item_id) == (None, None)


@pytest.mark.parametrize("replaced", [False, True])
async def test_clone(db_session, printer_factory, archive_factory, replaced):
    """A clone is an insert: its parent is locked even when the source row's live link names it."""
    from backend.app.services.queue_ops import clone_item

    _printer, _queue, original, _x, item_id = await a_finished_job(
        db_session, printer_factory, archive_factory, status="pending"
    )
    async with database.async_session() as db:
        await db.execute(update(PrintQueueItem).where(PrintQueueItem.id == item_id).values(archive_id=original))
        await db.commit()
    if replaced:
        await replace_the_original(original)
    async with database.async_session() as db:
        clone = await clone_item(db, item_id)
        clone_id = clone.id
    assert await link(clone_id) == ((None, None) if replaced else (original, None))


@pytest.mark.parametrize("replaced", [False, True])
async def test_plate_repeat(db_session, printer_factory, archive_factory, replaced):
    from backend.app.services.plate_hold import answer_by_repeating

    printer_id, _queue, original, _x, item_id = await a_finished_job(
        db_session, printer_factory, archive_factory, status="completed"
    )
    if replaced:
        await replace_the_original(original)
    async with database.async_session() as db:
        again = await answer_by_repeating(db, printer_id)
        assert again is not None and again.id == item_id
    assert await link(item_id) == ((None, None) if replaced else (original, None))
