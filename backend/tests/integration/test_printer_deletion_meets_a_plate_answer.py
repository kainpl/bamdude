"""Printer deletion with its archives meets a plate answer on that printer (G03 B, A19 / A21 / A26 H6-H7; acceptance
TA41 (b)).

``DELETE /printers/{P}?delete_archives=true`` hard-deletes P's archives first -- P's own gate on X does not keep X
there (``retired_printer_cleanup``) -- each in its own transaction, then removes P's queue rows, header and row in
one. The hard delete of X is stopped inside its file-path lock, before its rows; a plate answer on P without an
expected archive (a Clear) then waits on X's guard holding no printer row, and once X is gone answers
``StalePlateAnswer``. No hang; the deletion's last step takes P's row without waiting on anything the archive loop
left held.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import archive_write_scope
from backend.app.services.plate_hold import StalePlateAnswer
from backend.tests.integration.lane_pairs import held_inside, pending_rows
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_a_plate_answer_meeting_the_deletion_of_its_gated_archive_answers_stale(
    test_engine, db_session, printer_factory, archive_factory, async_client, monkeypatch
):
    from backend.app.services import plate_answers
    from backend.app.services.printer_manager import printer_manager

    printer_id, _ = await pending_rows(db_session, printer_factory, count=0)
    archive = await archive_factory(printer_id, status="completed")
    db_session.add(PrintQueueItem(queue_id=printer_id, position=0, status="completed", archive_id=archive.id))
    printer = await db_session.get(Printer, printer_id)
    printer.awaiting_plate_clear = True
    printer.awaiting_plate_clear_archive_id = archive.id
    printer.awaiting_plate_clear_token = "exact-owner"
    await db_session.commit()
    archive_id = archive.id
    monkeypatch.setattr(printer_manager, "confirm_awaiting_plate_clear_released", MagicMock())
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    reached, release = held_inside(monkeypatch, archive_write_scope, "archive_file_reference_scope")

    async def plate_answer():
        async with factory() as db:
            return await plate_answers.answer_plate_run(
                db, printer_id=printer_id, expected_archive_id=None, action="clear"
            )

    deleting = asyncio.create_task(async_client.delete(f"/api/v1/printers/{printer_id}?delete_archives=true"))
    answering = None
    try:
        await asyncio.wait_for(reached.wait(), 15)
        answering = asyncio.create_task(plate_answer())
        await asyncio.sleep(0.5)
        assert not answering.done(), "the plate answer waits on X's guard"
        release.set()
        response = await asyncio.wait_for(deleting, 30)
        try:
            answered = await asyncio.wait_for(answering, 30)
        except StalePlateAnswer as stale:
            answered = stale
    finally:
        release.set()
        await asyncio.gather(deleting, *([answering] if answering else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    assert isinstance(answered, StalePlateAnswer), answered
    async with factory() as db:
        assert await db.get(PrintArchive, archive_id) is None
        assert await db.get(Printer, printer_id) is None


async def test_the_printer_deletion_meeting_a_plate_clear_inside_its_lanes_finishes(
    test_engine, db_session, printer_factory, archive_factory, async_client, monkeypatch
):
    """TA26: a plate Clear holding the printer's lanes (it then locks the printer row) against the deletion of that
    printer (its printer row early, then the lanes): no cycle on PostgreSQL either -- the deletion takes the printer
    row FOR NO KEY UPDATE, the Clear its own lock of the same mode, and the deletion waits for the lane."""
    from backend.app.services import plate_answers
    from backend.app.services.printer_manager import printer_manager
    from backend.tests.integration.lane_pairs import bypass_the_process_lane

    printer_id, _ = await pending_rows(db_session, printer_factory, count=0)
    archive = await archive_factory(printer_id, status="completed")
    db_session.add(PrintQueueItem(queue_id=printer_id, position=0, status="completed", archive_id=archive.id))
    printer = await db_session.get(Printer, printer_id)
    printer.awaiting_plate_clear = True
    printer.awaiting_plate_clear_archive_id = archive.id
    printer.awaiting_plate_clear_token = "exact-owner"
    await db_session.commit()
    archive_id = archive.id
    monkeypatch.setattr(printer_manager, "confirm_awaiting_plate_clear_released", MagicMock())
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, printer_id)
    reached, release = held_inside(monkeypatch, plate_answers, "claim_lanes")

    async def plate_clear():
        async with factory() as db:
            return await plate_answers.answer_plate_run(
                db, printer_id=printer_id, expected_archive_id=archive_id, action="clear"
            )

    clearing = asyncio.create_task(plate_clear())
    deleting = None
    try:
        await asyncio.wait_for(reached.wait(), 15)
        deleting = asyncio.create_task(async_client.delete(f"/api/v1/printers/{printer_id}?delete_archives=false"))
        await asyncio.sleep(0.5)
        assert not deleting.done(), "the deletion waits for the Clear's lane"
        release.set()
        result = await asyncio.wait_for(clearing, 30)
        response = await asyncio.wait_for(deleting, 30)
    finally:
        release.set()
        await asyncio.gather(clearing, *([deleting] if deleting else []), return_exceptions=True)
    assert result.archive.id == archive_id
    assert response.status_code == 200, response.text
    async with factory() as db:
        assert await db.get(Printer, printer_id) is None
