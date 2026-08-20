"""A "Print now" already on its way must stop the queue dispatching over it.

Reported against 0.5.4: send file1 to a printer with **Print now**, then add
file2 to that printer's queue while file1 has not started yet — file2 goes out
instead, and file1 never prints.

The window is between pressing the button and the printer reporting the job:
FTP upload, an optional preheat soak, ``start_print``, then the printer's own
run-up, which on an H2D sits in FINISH for 80–210 s. In that window a direct
print used to hold nothing the queue could see — ``PrinterQueue.status`` is
written by ``on_print_start``, i.e. once the printer has already started, and
``_is_printer_idle`` reads live MQTT, which is exactly what lags. So the claim
is now taken at dispatch, and this file pins that the queue respects it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.print_scheduler import PrintScheduler
from backend.app.services.queue_batch import claim_printer_for_direct_print


async def _printer_with_pending_item(db_session, printer_factory):
    """A printer whose queue holds file2, waiting."""
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()

    item = PrintQueueItem(queue_id=queue.id, status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    return printer, item


@pytest.fixture
def scheduler(monkeypatch, db_session):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.services.print_scheduler.async_session", _session_ctx)
    return PrintScheduler()


def _idle_printer_manager():
    """The printer as MQTT reports it during someone else's dispatch: idle."""
    return patch.multiple(
        "backend.app.services.print_scheduler.printer_manager",
        is_connected=lambda pid: True,
        get_status=lambda pid: SimpleNamespace(state="IDLE", raw_data={}, subtask_id=None, gcode_file=None),
        is_awaiting_plate_clear=lambda pid: False,
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_queue_waits_for_a_direct_print(db_session, printer_factory, scheduler):
    """The bug, end to end: file1 claimed, file2 must not overtake it."""
    printer, item = await _printer_with_pending_item(db_session, printer_factory)
    await claim_printer_for_direct_print(db_session, printer_id=printer.id)

    start = AsyncMock()
    with patch.object(PrintScheduler, "_start_print", start), _idle_printer_manager():
        await scheduler.check_queue()

    await db_session.refresh(item)
    assert start.await_count == 0, "file2 was dispatched over a print already on its way"
    assert item.status == "pending", "file2 must still be owed"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_accepting_a_direct_print_is_what_takes_the_claim(db_session, printer_factory, monkeypatch):
    """The wiring, without which the test above would pass on a contract nobody honours.

    ⚠️ The claim is taken inside ``_dispatch``, after its refusals and while it
    still holds the lock — so a rejected dispatch cannot leave a claim behind,
    and an accepted one is claimed before the caller gets its job id back.
    """
    from contextlib import asynccontextmanager

    from backend.app.services.background_dispatch import BackgroundDispatchService

    printer, _ = await _printer_with_pending_item(db_session, printer_factory)

    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.services.background_dispatch.async_session", _session_ctx)

    service = BackgroundDispatchService()
    with (
        patch("backend.app.services.background_dispatch.printer_manager.get_status", return_value=None),
        patch("backend.app.services.background_dispatch.ws_manager.broadcast", new_callable=AsyncMock),
    ):
        await service.dispatch_print_library_file(
            file_id=22,
            filename="file1.gcode.3mf",
            printer_id=printer.id,
            printer_name=printer.name,
            options={"plate_id": 2},
            requested_by_user_id=None,
            requested_by_username=None,
        )

    job = service._queued_jobs[0]
    assert job.queue_item_id is not None, "the job must carry its claim, or nothing releases it"
    assert job.awaited_by_scheduler is False, "the dispatcher owns this item, not the scheduler"

    claimed = await db_session.get(PrintQueueItem, job.queue_item_id)
    assert claimed.status == "printing"
    assert claimed.library_file_id == 22
    assert claimed.plate_id == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_rejected_direct_print_leaves_no_claim(db_session, printer_factory, monkeypatch):
    """⚠️ Ordering, pinned: claiming before the refusals would park a printer
    every time somebody pressed Print now on a busy one."""
    from contextlib import asynccontextmanager

    from backend.app.services.background_dispatch import BackgroundDispatchService, DispatchEnqueueRejected

    printer, _ = await _printer_with_pending_item(db_session, printer_factory)

    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.services.background_dispatch.async_session", _session_ctx)

    service = BackgroundDispatchService()
    with (
        patch(
            "backend.app.services.background_dispatch.printer_manager.get_status",
            return_value=SimpleNamespace(state="RUNNING", gcode_file="something.gcode.3mf"),
        ),
        pytest.raises(DispatchEnqueueRejected),
    ):
        await service.dispatch_print_library_file(
            file_id=22,
            filename="file1.gcode.3mf",
            printer_id=printer.id,
            printer_name=printer.name,
            options={},
            requested_by_user_id=None,
            requested_by_username=None,
        )

    printing = (
        (
            await db_session.execute(
                select(PrintQueueItem)
                .where(PrintQueueItem.queue_id == printer.id)
                .where(PrintQueueItem.status == "printing")
            )
        )
        .scalars()
        .all()
    )
    assert printing == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_another_printers_direct_print_does_not_hold_this_queue(db_session, printer_factory, scheduler):
    """⚠️ Per printer. A farm-wide hold would be worse than the bug — one
    Print now anywhere would stall every queue until it started."""
    printer, item = await _printer_with_pending_item(db_session, printer_factory)
    other = await printer_factory(name="other")
    db_session.add(PrinterQueue(id=other.id, printer_id=other.id))
    await db_session.commit()
    await claim_printer_for_direct_print(db_session, printer_id=other.id)

    start = AsyncMock()
    with patch.object(PrintScheduler, "_start_print", start), _idle_printer_manager():
        await scheduler.check_queue()

    assert start.await_count == 1, "an unrelated printer's dispatch must not block this queue"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_idle_printer_still_takes_work(db_session, printer_factory, scheduler):
    """The baseline the other two are measured against — without it, "block
    everything" would pass this file."""
    printer, item = await _printer_with_pending_item(db_session, printer_factory)

    start = AsyncMock()
    with patch.object(PrintScheduler, "_start_print", start), _idle_printer_manager():
        await scheduler.check_queue()

    assert start.await_count == 1
