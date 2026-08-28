"""What is claimed must be released — on every way out, not just the happy one.

⚠️ This is the one new failure mode the claim introduces. A leaked claim is
worse than the bug it fixes: ``check_queue`` seeds ``busy_printers`` from
``PrinterQueue.status='printing'`` with no age check and no cross-check against
the printer, so a claim nobody released reads as live for ever and the printer
silently stops taking work. m120's docstring already names that outcome.
"""

from datetime import datetime, timezone

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.background_dispatch import PrintDispatchJob, background_dispatch


async def _claimed(db_session, printer_factory, **job_kwargs):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="printing")
    db_session.add(queue)
    await db_session.flush()
    item = PrintQueueItem(queue_id=queue.id, status="printing", position=0, started_at=datetime.now(timezone.utc))
    db_session.add(item)
    await db_session.flush()
    queue.current_item_id = item.id
    await db_session.commit()

    job = PrintDispatchJob(
        id=1,
        kind="print_library_file",
        source_id=1,
        source_name="file1.gcode.3mf",
        printer_id=printer.id,
        printer_name=printer.name,
        queue_item_id=item.id,
        **job_kwargs,
    )
    return queue, item, job


@pytest.fixture
def dispatch_db(monkeypatch, db_session):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.services.background_dispatch.async_session", _session_ctx)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_failed_dispatch_gives_the_printer_back(db_session, printer_factory, dispatch_db):
    queue, item, job = await _claimed(db_session, printer_factory)
    job.outcome = {"success": False, "archive_id": None, "error": "FTP 553", "cancelled": False}

    await background_dispatch._release_direct_claim(job, status="failed")

    await db_session.refresh(item)
    await db_session.refresh(queue)
    assert item.status == "failed"
    assert item.completed_at is not None
    assert item.error_message == "FTP 553", "the only surviving diagnostic for whoever pressed the button"
    assert queue.status != "printing", "the printer must be dispatchable again"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_cancelled_dispatch_gives_the_printer_back(db_session, printer_factory, dispatch_db):
    queue, item, job = await _claimed(db_session, printer_factory)

    await background_dispatch._release_direct_claim(job, status="cancelled")

    await db_session.refresh(item)
    await db_session.refresh(queue)
    assert item.status == "cancelled"
    assert queue.status != "printing"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_scheduler_owned_item_is_left_alone(db_session, printer_factory, dispatch_db):
    """⚠️ The scheduler has its own _fail_item / _cancel_item / defer-if-busy
    handling, including the #2598 busy-refusal that puts the item back to
    pending rather than failing it. Two owners for one row is how that gets
    silently overwritten."""
    queue, item, job = await _claimed(db_session, printer_factory, awaited_by_scheduler=True)

    await background_dispatch._release_direct_claim(job, status="failed")

    await db_session.refresh(item)
    assert item.status == "printing", "the scheduler decides what happens to its own item"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_job_that_claimed_nothing_is_a_noop(db_session, printer_factory, dispatch_db):
    """A printer with no queue row dispatches unclaimed — releasing must not raise."""
    printer = await printer_factory()
    job = PrintDispatchJob(
        id=1,
        kind="print_library_file",
        source_id=1,
        source_name="f",
        printer_id=printer.id,
        printer_name=printer.name,
    )

    await background_dispatch._release_direct_claim(job, status="failed")  # no error


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_runner_releases_when_the_dispatch_raises(db_session, printer_factory, dispatch_db):
    """The wiring. Every failure inside the runners raises — including a refused
    ``start_print``, which becomes ``RuntimeError("Failed to start print")`` — so
    this is the path a real upload failure takes out of the dispatcher."""
    from unittest.mock import AsyncMock, patch

    queue, item, job = await _claimed(db_session, printer_factory)

    async def _boom(_self, _job):
        raise RuntimeError("FTP upload failed")

    with (
        patch.object(type(background_dispatch), "_process_job", _boom),
        patch.object(type(background_dispatch), "_mark_job_finished", AsyncMock()),
    ):
        await background_dispatch._run_active_job(job)

    await db_session.refresh(item)
    await db_session.refresh(queue)
    assert item.status == "failed"
    assert queue.status != "printing", "the printer stayed claimed after a failed upload"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_runner_holds_the_claim_when_the_print_actually_started(db_session, printer_factory, dispatch_db):
    """⚠️ The other half, and the reason the release is not in ``finally``: on
    success the claim now belongs to the running print. Releasing here would
    hand the printer to the next queued job mid-print."""
    from unittest.mock import AsyncMock, patch

    queue, item, job = await _claimed(db_session, printer_factory)

    async def _ok(_self, _job):
        _job.outcome = {"success": True, "archive_id": 1, "error": None, "cancelled": False}

    with (
        patch.object(type(background_dispatch), "_process_job", _ok),
        patch.object(type(background_dispatch), "_mark_job_finished", AsyncMock()),
    ):
        await background_dispatch._run_active_job(job)

    await db_session.refresh(item)
    await db_session.refresh(queue)
    assert item.status == "printing"
    assert queue.status == "printing"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_release_that_goes_wrong_does_not_replace_the_real_error(db_session, printer_factory, monkeypatch):
    """⚠️ This runs on the way out of a dispatch that has already failed. Raising
    here would swallow the reason the dispatch failed and replace it with a
    secondary one from the cleanup."""
    from contextlib import asynccontextmanager

    printer = await printer_factory()

    @asynccontextmanager
    async def _exploding_ctx():
        raise RuntimeError("database is on fire")
        yield  # pragma: no cover

    monkeypatch.setattr("backend.app.services.background_dispatch.async_session", _exploding_ctx)

    job = PrintDispatchJob(
        id=1,
        kind="print_library_file",
        source_id=1,
        source_name="f",
        printer_id=printer.id,
        printer_name=printer.name,
        queue_item_id=99,
    )

    await background_dispatch._release_direct_claim(job, status="failed")  # no error
