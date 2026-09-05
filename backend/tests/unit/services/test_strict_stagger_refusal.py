"""Strict mode refuses a direct print instead of making it wait; the queue is never refused.

``stagger_strict_for_direct_dispatch`` used to be a check inside the runners,
i.e. after ``_process_job`` had already parked the print in
``acquire_stagger_slot`` until a slot freed — so the toggle could only fire on a
race and never meant what its own description promised. The decision it now
carries (user, 2026-09-05): if the queue is waiting, shoving a print in directly
is not acceptable, so ON refuses a direct print outright and OFF lets it wait.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.background_dispatch import BackgroundDispatchService, PrintDispatchJob

REASON = "Stagger cap reached — wait for a free slot or disable stagger_strict_for_direct_dispatch"


@pytest.fixture
def service():
    svc = BackgroundDispatchService()
    svc._release_direct_claim = AsyncMock()
    svc._mark_job_finished = AsyncMock()
    svc._run_reprint_archive = AsyncMock()
    svc._run_print_library_file = AsyncMock()
    return svc


@pytest.fixture
def job_factory():
    def _make(*, kind="reprint_archive", awaited_by_scheduler=False) -> PrintDispatchJob:
        return PrintDispatchJob(
            id=7,
            kind=kind,
            source_id=100,
            source_name="gearbox.gcode.3mf",
            printer_id=3,
            printer_name="Printer C",
            queue_item_id=55,
            awaited_by_scheduler=awaited_by_scheduler,
        )

    return _make


@dataclass
class _Patches:
    """The three outside answers a refusal decision depends on, plus the spies."""

    acquire: AsyncMock
    blocks: AsyncMock
    report_failure: AsyncMock
    _settings: SimpleNamespace

    def strict(self, value: str | None) -> None:
        self._settings.value = value

    def settings_read_raises(self) -> None:
        self._settings.boom = True


@pytest.fixture
def patches():
    """Everything ``_process_job`` reaches outside itself, under one handle."""
    state = SimpleNamespace(value="false", boom=False)

    async def _get_setting(_db, _key):
        if state.boom:
            raise RuntimeError("settings table is unreadable")
        return state.value

    @asynccontextmanager
    async def _session():
        yield SimpleNamespace()

    acquire = AsyncMock()
    blocks = AsyncMock(return_value=False)
    report_failure = AsyncMock()

    with (
        patch("backend.app.services.background_dispatch.async_session", _session),
        patch("backend.app.services.background_dispatch.report_failure_if_unwatched", report_failure),
        patch("backend.app.api.routes.settings.get_setting", _get_setting),
        patch("backend.app.services.print_scheduler.scheduler.acquire_stagger_slot", acquire),
        patch("backend.app.services.print_scheduler.scheduler.stagger_blocks", blocks),
    ):
        yield _Patches(acquire=acquire, blocks=blocks, report_failure=report_failure, _settings=state)


def _assert_nothing_was_refused(service, patches, job):
    service._release_direct_claim.assert_not_awaited()
    patches.report_failure.assert_not_awaited()
    assert job.outcome == {}


@pytest.mark.asyncio
async def test_a_direct_print_is_refused_before_it_would_wait(service, job_factory, patches):
    job = job_factory(kind="reprint_archive", awaited_by_scheduler=False)
    patches.strict("true")
    patches.blocks.return_value = True

    await service._process_job(job)

    # Refusing after the wait would be no refusal at all.
    patches.acquire.assert_not_awaited()
    service._run_reprint_archive.assert_not_awaited()
    assert job.outcome == {"success": False, "archive_id": None, "error": REASON, "cancelled": False}
    # queue_error=False: the item failed, the queue did not (Ruling 13).
    service._release_direct_claim.assert_awaited_once_with(job, status="failed", queue_error=False)
    service._mark_job_finished.assert_awaited_once_with(job, failed=True, message=REASON)
    patches.report_failure.assert_awaited_once_with(job)
    assert job.completion_event.is_set()


@pytest.mark.asyncio
async def test_with_strict_off_a_direct_print_waits_as_before(service, job_factory, patches):
    """The default. A full group makes the print wait in ``acquire_stagger_slot``."""
    job = job_factory()
    patches.strict("false")
    patches.blocks.return_value = True

    await service._process_job(job)

    patches.acquire.assert_awaited_once_with(job.printer_id)
    service._run_reprint_archive.assert_awaited_once_with(job)
    _assert_nothing_was_refused(service, patches, job)


@pytest.mark.asyncio
async def test_a_queue_job_is_never_refused(service, job_factory, patches):
    """The queue waits by design, and its slot was pre-registered in ``_start_print``."""
    job = job_factory(kind="print_library_file", awaited_by_scheduler=True)
    patches.strict("true")
    patches.blocks.return_value = True

    await service._process_job(job)

    patches.acquire.assert_awaited_once_with(job.printer_id)
    service._run_print_library_file.assert_awaited_once_with(job)
    _assert_nothing_was_refused(service, patches, job)


@pytest.mark.asyncio
async def test_with_room_a_direct_print_proceeds(service, job_factory, patches):
    job = job_factory()
    patches.strict("true")
    patches.blocks.return_value = False

    await service._process_job(job)

    patches.acquire.assert_awaited_once_with(job.printer_id)
    service._run_reprint_archive.assert_awaited_once_with(job)
    _assert_nothing_was_refused(service, patches, job)


@pytest.mark.asyncio
async def test_a_broken_settings_read_never_refuses(service, job_factory, patches):
    """Strictness is a refinement — a read that fails must not refuse somebody's print."""
    job = job_factory()
    patches.settings_read_raises()
    patches.blocks.return_value = True

    await service._process_job(job)

    patches.acquire.assert_awaited_once_with(job.printer_id)
    service._run_reprint_archive.assert_awaited_once_with(job)
    _assert_nothing_was_refused(service, patches, job)


@pytest.mark.asyncio
async def test_a_started_print_is_still_marked_complete(service, job_factory, patches):
    """The guard's other direction: a runner that really printed must still be
    marked finished, with the wrapper's own completion message."""
    job = job_factory()
    patches.strict("false")

    async def _ran(_job):
        _job.outcome = {"success": True, "archive_id": 7, "error": None, "cancelled": False}

    service._run_reprint_archive = AsyncMock(side_effect=_ran)

    with patch("backend.app.services.background_dispatch.ws_manager.broadcast", new_callable=AsyncMock):
        await service._run_active_job(job)

    service._mark_job_finished.assert_awaited_once_with(job, failed=False, message="Background dispatch complete")


@pytest.mark.asyncio
async def test_a_refused_job_is_not_also_marked_complete(service, job_factory, patches):
    """⚠️ ``_run_active_job`` marks the job finished after ``_process_job`` returns.

    A refusal returns normally, so an unguarded mark would count the same job in
    both batch tallies and leave "completed" as the last word on a print that
    never ran — the opposite of what the refusal is for.
    """
    job = job_factory()
    patches.strict("true")
    patches.blocks.return_value = True

    with patch("backend.app.services.background_dispatch.ws_manager.broadcast", new_callable=AsyncMock):
        await service._run_active_job(job)

    service._mark_job_finished.assert_awaited_once_with(job, failed=True, message=REASON)


@pytest.fixture
def dispatch_db(monkeypatch, db_session):
    """Point the release path's own session at the test engine."""

    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.services.background_dispatch.async_session", _session_ctx)


async def _real_claim(db_session, printer_factory):
    """The claim a direct print actually takes at submit, through its own writer."""
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.queue_batch import claim_printer_for_direct_print

    printer = await printer_factory()
    item = await claim_printer_for_direct_print(db_session, printer_id=printer.id, origin="direct")
    await db_session.commit()
    queue = await db_session.get(PrinterQueue, item.queue_id)
    job = PrintDispatchJob(
        id=1,
        kind="reprint_archive",
        source_id=1,
        source_name="gearbox.gcode.3mf",
        printer_id=printer.id,
        printer_name=printer.name,
        queue_item_id=item.id,
    )
    job.outcome = {"success": False, "archive_id": None, "error": REASON, "cancelled": False}
    return queue, item, job


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_refusal_fails_the_item_and_leaves_the_queue_idle(db_session, printer_factory, dispatch_db):
    """⚠️ Ruling 13. ``check_queue`` skips every item in a queue whose status is
    ``error``, so failing the queue for a refusal would freeze exactly the queue
    strict mode exists to protect."""
    service = BackgroundDispatchService()
    queue, item, job = await _real_claim(db_session, printer_factory)

    await service._release_direct_claim(job, status="failed", queue_error=False)

    await db_session.refresh(item)
    await db_session.refresh(queue)
    assert item.status == "failed"
    assert item.error_message == REASON
    assert item.completed_at is not None
    assert queue.status == "idle", "a 'not now' is not a hardware failure"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_real_failure_still_puts_the_queue_in_error(db_session, printer_factory, dispatch_db):
    """The default did not move: a dispatch that broke still stops the queue."""
    service = BackgroundDispatchService()
    queue, item, job = await _real_claim(db_session, printer_factory)

    await service._release_direct_claim(job, status="failed")

    await db_session.refresh(queue)
    assert queue.status == "error"


def test_the_runners_no_longer_carry_a_strict_block():
    """The check lives in ``_process_job`` alone; the runners are back to printing."""
    from pathlib import Path

    import backend.app.services.background_dispatch as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    # A deletion guard for the two removed in-runner blocks, text-based on purpose — drop it if a
    # second legitimate ``stagger_blocks(`` call site is ever added.
    assert source.count("_strict_stagger_refuses") == 2  # the definition and its one call
    assert source.count("stagger_blocks(") == 1  # only inside _strict_stagger_refuses
    assert source.count("stagger_strict_for_direct_dispatch") == 2  # the setting read and the refusal message
