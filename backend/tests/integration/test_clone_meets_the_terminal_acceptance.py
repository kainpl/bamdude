"""A clone of a printing external-print row meets the acceptance of that run's terminal, both orders (G03 B, A27 H3;
acceptance TA40).

The row's only source is its execution archive X. The clone takes X ``FOR KEY SHARE`` before any job row (L5); the
real ``_accept_bound_terminal_run`` takes X under its write scope, then the row. Whichever holds X, the other waits on
X -- never the acceptance on the row behind a clone that waits on X -- and both commit; the copy names X.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import queue_source_link
from backend.tests.integration.lane_pairs import (
    a_waiter_appears,
    bypass_the_process_lane,
    held_after,
    pending_rows,
)
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

T0 = datetime(2026, 11, 20, 10, 0, 0)
ANY_WAIT = ("transactionid", "tuple", "advisory")


async def an_external_print(db_session, printer_factory, archive_factory) -> tuple[int, int, int]:
    queue_id, _ = await pending_rows(db_session, printer_factory, count=0)
    execution = await archive_factory(queue_id, status="printing")
    row = PrintQueueItem(queue_id=queue_id, position=0, status="printing", started_at=T0, archive_id=execution.id)
    db_session.add(row)
    await db_session.flush()
    header = await db_session.get(PrinterQueue, queue_id)
    header.status, header.current_item_id = "printing", row.id
    await db_session.commit()
    return queue_id, execution.id, row.id


@pytest.fixture
def acceptance(test_engine, monkeypatch):
    from backend.app import main
    from backend.app.services.print_run_binding import PrintRunBinding

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(main, "async_session", factory)

    async def accept(printer_id, archive_id, item_id):
        binding = PrintRunBinding(printer_id=printer_id, archive_id=archive_id, queue_item_id=item_id)
        return await main._accept_bound_terminal_run(printer_id, binding, {"gcode_state": "FINISH"})

    return factory, accept


async def outcome(factory, archive_id, item_id):
    async with factory() as db:
        archive = await db.get(PrintArchive, archive_id)
        copies = list(
            await db.scalars(
                select(PrintQueueItem).where(PrintQueueItem.id != item_id, PrintQueueItem.status == "pending")
            )
        )
    assert "bamdude_terminal_acceptance" in (archive.extra_data or {}), "the acceptance committed"
    assert [copy.archive_id for copy in copies] == [archive_id], "the copy names the execution archive"


async def test_a_clone_meeting_the_acceptance_that_holds_x_waits_on_x(
    test_engine, db_session, printer_factory, archive_factory, async_client, acceptance, monkeypatch
):
    from backend.app import main

    factory, accept = acceptance
    queue_id, archive_id, item_id = await an_external_print(db_session, printer_factory, archive_factory)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, main, "_bound_printing_item")  # X held, the row not yet locked
    accepting = asyncio.create_task(accept(queue_id, archive_id, item_id))
    cloning = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        cloning = asyncio.create_task(async_client.post(f"/api/v1/queue/{item_id}/clone"))
        await a_waiter_appears(test_engine, factory, cloning, on=ANY_WAIT)
        release.set()
        await asyncio.wait_for(accepting, 20)
        response = await asyncio.wait_for(cloning, 20)
    finally:
        release.set()
        await asyncio.gather(accepting, *([cloning] if cloning else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    await outcome(factory, archive_id, item_id)


async def test_the_acceptance_meeting_a_clone_that_holds_x_waits_on_x(
    test_engine, db_session, printer_factory, archive_factory, async_client, acceptance, monkeypatch
):
    factory, accept = acceptance
    queue_id, archive_id, item_id = await an_external_print(db_session, printer_factory, archive_factory)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, queue_source_link, "lock_parent")  # X FOR KEY SHARE, before rows
    cloning = asyncio.create_task(async_client.post(f"/api/v1/queue/{item_id}/clone"))
    accepting = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        accepting = asyncio.create_task(accept(queue_id, archive_id, item_id))
        await a_waiter_appears(test_engine, factory, accepting, on=ANY_WAIT)
        release.set()
        response = await asyncio.wait_for(cloning, 20)
        await asyncio.wait_for(accepting, 20)
    finally:
        release.set()
        await asyncio.gather(cloning, *([accepting] if accepting else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    await outcome(factory, archive_id, item_id)
