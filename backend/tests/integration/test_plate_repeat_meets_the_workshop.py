"""Plate Repeat restoring a job's link to its original meets the Workshop's filing of the same prints, both orders
(G03 B, A21 / A24 / A27; acceptance TA30 for plate Repeat).

The gate's row was captured from archive A (its provenance stamped) and its run printed as X. Repeat re-arms the row
and restores its link to A only while A is the stamped incarnation: the answer takes X and A under its guards and A
``FOR KEY SHARE`` before the row. ``POST /projects/{id}/add-archives`` of ``[A, X]`` takes them ``FOR UPDATE``,
ascending. Whichever goes first, the other waits on an archive -- never on the row -- and both commit.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.queue_source import QueueSource
from backend.app.services import queue_source_link
from backend.tests.integration.lane_pairs import bypass_the_process_lane, held_after, pending_rows
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def a_gated_repeat(db_session, printer_factory, archive_factory, committing_client):
    queue_id, _ = await pending_rows(db_session, printer_factory, count=0)
    original = await archive_factory(queue_id, status="completed")
    execution = await archive_factory(queue_id, status="completed")
    blob = QueueSource(sha256="f" * 64, size_bytes=1, relative_path="objects/ff/job.3mf", format="3mf", state="ready")
    db_session.add(blob)
    await db_session.flush()
    row = PrintQueueItem(
        queue_id=queue_id,
        position=0,
        status="completed",
        archive_id=execution.id,
        queue_source_id=blob.id,
        source_snapshot={
            "version": 1,
            "provenance": {"kind": "archive", "id": original.id, "incarnation": original.recovery_identity},
        },
    )
    db_session.add(row)
    printer = await db_session.get(Printer, queue_id)
    printer.awaiting_plate_clear = True
    printer.awaiting_plate_clear_archive_id = execution.id
    printer.awaiting_plate_clear_token = "exact-owner"
    await db_session.commit()
    created = await committing_client.post("/api/v1/projects/", json={"name": "Filed"})
    assert created.status_code in (200, 201), created.text
    return created.json()["id"], queue_id, original.id, execution.id, row.id


@pytest.fixture
def released(monkeypatch):
    from backend.app.services.printer_manager import printer_manager

    monkeypatch.setattr(printer_manager, "confirm_awaiting_plate_clear_released", MagicMock())


async def repeat(factory, printer_id, execution_id):
    from backend.app.services import plate_answers

    async with factory() as db:
        return await plate_answers.answer_plate_run(
            db, printer_id=printer_id, expected_archive_id=execution_id, action="repeat", may_file_future=True
        )


def file(client, order_id, original_id, execution_id):
    return client.post(f"/api/v1/projects/{order_id}/add-archives", json={"archive_ids": [original_id, execution_id]})


async def outcome(factory, order_id, original_id, execution_id, row_id):
    async with factory() as db:
        row = await db.get(PrintQueueItem, row_id)
        a = await db.get(PrintArchive, original_id)
        x = await db.get(PrintArchive, execution_id)
    assert (row.status, row.archive_id) == ("pending", original_id), "re-armed, its link restored to the original"
    assert (a.project_id, x.project_id) == (order_id, order_id)


async def test_the_filing_meeting_a_repeat_that_holds_the_original_waits_on_an_archive(
    test_engine, db_session, printer_factory, archive_factory, committing_client, released, monkeypatch
):
    order_id, queue_id, original_id, execution_id, row_id = await a_gated_repeat(
        db_session, printer_factory, archive_factory, committing_client
    )
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, queue_source_link, "lock_parent")
    repeating = asyncio.create_task(repeat(factory, queue_id, execution_id))
    filing = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        filing = asyncio.create_task(file(committing_client, order_id, original_id, execution_id))
        await asyncio.sleep(0.5)
        assert not filing.done(), "the filing waits for the Repeat's archives"
        release.set()
        await asyncio.wait_for(repeating, 20)
        response = await asyncio.wait_for(filing, 20)
    finally:
        release.set()
        await asyncio.gather(repeating, *([filing] if filing else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    await outcome(factory, order_id, original_id, execution_id, row_id)


async def test_a_repeat_meeting_the_filing_that_holds_the_archives_waits_then_restores(
    test_engine, db_session, printer_factory, archive_factory, committing_client, released, monkeypatch
):
    from backend.app.api.routes import projects

    order_id, queue_id, original_id, execution_id, row_id = await a_gated_repeat(
        db_session, printer_factory, archive_factory, committing_client
    )
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, projects, "lock_prints")
    filing = asyncio.create_task(file(committing_client, order_id, original_id, execution_id))
    repeating = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        repeating = asyncio.create_task(repeat(factory, queue_id, execution_id))
        await asyncio.sleep(0.5)
        assert not repeating.done(), "the Repeat waits for the filing's archives"
        release.set()
        response = await asyncio.wait_for(filing, 20)
        await asyncio.wait_for(repeating, 20)
    finally:
        release.set()
        await asyncio.gather(filing, *([repeating] if repeating else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    await outcome(factory, order_id, original_id, execution_id, row_id)
