"""A plate answer meets the claim of the printer's next job, both orders (G03 B, A21; acceptance TA26 "plate Repeat and
Clear vs the claim").

The plate answer takes the answered archive's guard and the printer's lanes in a clean session, then the gate's row;
the claim takes the lane, the header and its row. Whichever holds the lane, the other waits on it -- never on the
header or a row -- and both commit: the gate is released, the finished row is cleaned up, the next job claimed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.tests.integration.lane_pairs import (
    a_waiter_appears,
    bypass_the_process_lane,
    claim_holding_the_lane,
    go,
    held_inside,
    pending_rows,
)
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def a_gated_printer(db_session, printer_factory, archive_factory) -> tuple[int, int, int, int]:
    queue_id, (next_id,) = await pending_rows(db_session, printer_factory, count=1)
    archive = await archive_factory(queue_id, status="completed")
    finished = PrintQueueItem(queue_id=queue_id, position=0, status="completed", archive_id=archive.id)
    db_session.add(finished)
    printer = await db_session.get(Printer, queue_id)
    printer.awaiting_plate_clear = True
    printer.awaiting_plate_clear_archive_id = archive.id
    printer.awaiting_plate_clear_token = "exact-owner"
    await db_session.commit()
    return queue_id, archive.id, finished.id, next_id


@pytest.fixture
def released(monkeypatch):
    from backend.app.services.printer_manager import printer_manager

    monkeypatch.setattr(printer_manager, "confirm_awaiting_plate_clear_released", MagicMock())


async def clear(factory, printer_id, archive_id):
    from backend.app.services import plate_answers

    async with factory() as db:
        return await plate_answers.answer_plate_run(
            db, printer_id=printer_id, expected_archive_id=archive_id, action="clear"
        )


async def outcome(factory, printer_id, finished_id, next_id):
    async with factory() as db:
        assert not (await db.get(Printer, printer_id)).awaiting_plate_clear, "the gate is released"
        assert await db.get(PrintQueueItem, finished_id) is None, "the finished row is cleaned up"
        assert (await db.get(PrintQueueItem, next_id)).status == "printing", "the next job is claimed"


async def test_a_plate_clear_meeting_a_claim_waits_on_the_lane(
    test_engine, db_session, printer_factory, archive_factory, released, monkeypatch
):
    printer_id, archive_id, finished_id, next_id = await a_gated_printer(db_session, printer_factory, archive_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, printer_id)
    reached, release = asyncio.Event(), asyncio.Event()
    claim = asyncio.create_task(claim_holding_the_lane(factory, printer_id, next_id, reached, release))
    clearing = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        clearing = asyncio.create_task(clear(factory, printer_id, archive_id))
        await a_waiter_appears(test_engine, factory, clearing, printer_id)
        release.set()
        assert await asyncio.wait_for(claim, 20) == 1
        result = await asyncio.wait_for(clearing, 20)
    finally:
        release.set()
        await asyncio.gather(claim, *([clearing] if clearing else []), return_exceptions=True)
    assert result.archive.id == archive_id
    await outcome(factory, printer_id, finished_id, next_id)


async def test_a_claim_meeting_a_plate_clear_waits_on_the_lane(
    test_engine, db_session, printer_factory, archive_factory, released, monkeypatch
):
    printer_id, archive_id, finished_id, next_id = await a_gated_printer(db_session, printer_factory, archive_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, printer_id)
    from backend.app.services import plate_answers

    # The plate answer binds claim_lanes at its import.
    reached, release = held_inside(monkeypatch, plate_answers, "claim_lanes")
    clearing = asyncio.create_task(clear(factory, printer_id, archive_id))
    claim = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        claim = asyncio.create_task(claim_holding_the_lane(factory, printer_id, next_id, asyncio.Event(), go()))
        await a_waiter_appears(test_engine, factory, claim, printer_id)
        release.set()
        result = await asyncio.wait_for(clearing, 20)
        assert await asyncio.wait_for(claim, 20) == 1
    finally:
        release.set()
        await asyncio.gather(clearing, *([claim] if claim else []), return_exceptions=True)
    assert result.archive.id == archive_id
    await outcome(factory, printer_id, finished_id, next_id)
