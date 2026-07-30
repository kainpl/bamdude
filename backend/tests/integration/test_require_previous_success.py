"""The ``require_previous_success`` gate (m116).

The flag was on ``AutoQueueItem`` from the two-tier split onwards, accepted and
stored and returned by the API — and read by nothing. "Don't run this if the
last print failed" is a safety option, and one that silently does nothing is
worse than one that is absent, because the operator believes the farm is
guarded. m116 puts the column back on the per-printer tier and both schedulers
now honour it.

What is pinned here is mostly the *lookback*, because that is where upstream
spent two bug reports:

* ``cancelled`` is neutral — stopping a print is a decision, not a failure.
* ``skipped`` is out of the lookback entirely — a skip was never a print
  attempt, and counting one as a failed predecessor is what let a single
  cancellation block 18 items over three days for their reporter.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.api.routes.print_queue import _acknowledge_blocking_failure
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.print_scheduler import PrintScheduler

BASE = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


async def _printer_with_queue(db_session, printer_factory, **kwargs):
    printer = await printer_factory(**kwargs)
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()
    await db_session.refresh(queue)
    return printer, queue


async def _finished(db_session, queue, status: str, minutes_ago: int, **kwargs) -> PrintQueueItem:
    """A queue row that has already finished, at a known point in the past."""
    item = PrintQueueItem(
        queue_id=queue.id,
        status=status,
        position=0,
        completed_at=BASE - timedelta(minutes=minutes_ago),
        **kwargs,
    )
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    return item


class TestTheLookback:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_printer_with_no_history_passes(self, db_session, printer_factory) -> None:
        """A gate must never block the first job on a fresh printer."""
        printer, _ = await _printer_with_queue(db_session, printer_factory)
        assert await PrintScheduler().previous_print_succeeded(db_session, printer.id) is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_completed_print_passes(self, db_session, printer_factory) -> None:
        printer, queue = await _printer_with_queue(db_session, printer_factory)
        await _finished(db_session, queue, "completed", minutes_ago=10)
        assert await PrintScheduler().previous_print_succeeded(db_session, printer.id) is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_failed_print_blocks(self, db_session, printer_factory) -> None:
        printer, queue = await _printer_with_queue(db_session, printer_factory)
        await _finished(db_session, queue, "completed", minutes_ago=60)
        await _finished(db_session, queue, "failed", minutes_ago=10)
        assert await PrintScheduler().previous_print_succeeded(db_session, printer.id) is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_cancelled_print_is_neutral(self, db_session, printer_factory) -> None:
        """Upstream #1667: a person stopping a print is a decision, not a fault,
        so what is queued behind it still runs."""
        printer, queue = await _printer_with_queue(db_session, printer_factory)
        await _finished(db_session, queue, "cancelled", minutes_ago=10)
        assert await PrintScheduler().previous_print_succeeded(db_session, printer.id) is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_skip_cannot_hide_the_failure_behind_it(self, db_session, printer_factory) -> None:
        """A skip is not a print attempt, so it is out of the lookback — the
        failure underneath is still what the gate reads."""
        printer, queue = await _printer_with_queue(db_session, printer_factory)
        await _finished(db_session, queue, "failed", minutes_ago=20)
        await _finished(db_session, queue, "skipped", minutes_ago=5)
        assert await PrintScheduler().previous_print_succeeded(db_session, printer.id) is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_acknowledged_failure_stops_blocking(self, db_session, printer_factory) -> None:
        printer, queue = await _printer_with_queue(db_session, printer_factory)
        await _finished(db_session, queue, "completed", minutes_ago=60)
        await _finished(db_session, queue, "failed", minutes_ago=10, gate_acknowledged=True)
        assert await PrintScheduler().previous_print_succeeded(db_session, printer.id) is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_later_success_releases_the_gate_on_its_own(self, db_session, printer_factory) -> None:
        """No acknowledgement needed when the printer has proved itself since."""
        printer, queue = await _printer_with_queue(db_session, printer_factory)
        await _finished(db_session, queue, "failed", minutes_ago=60)
        await _finished(db_session, queue, "completed", minutes_ago=10)
        assert await PrintScheduler().previous_print_succeeded(db_session, printer.id) is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_item_being_checked_is_excluded(self, db_session, printer_factory) -> None:
        """Guards against an item gating on its own row."""
        printer, queue = await _printer_with_queue(db_session, printer_factory)
        failed_self = await _finished(db_session, queue, "failed", minutes_ago=1)
        assert await PrintScheduler().previous_print_succeeded(db_session, printer.id, failed_self.id) is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_another_printers_failure_is_not_our_problem(self, db_session, printer_factory) -> None:
        clean, _ = await _printer_with_queue(db_session, printer_factory, name="clean")
        _, broken_queue = await _printer_with_queue(db_session, printer_factory, name="broken")
        await _finished(db_session, broken_queue, "failed", minutes_ago=5)
        assert await PrintScheduler().previous_print_succeeded(db_session, clean.id) is True


class TestTheGateStopsDispatch:
    """End-to-end through the real ``check_queue`` pass."""

    @pytest.fixture
    def scheduler(self, monkeypatch, db_session):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _session_ctx():
            yield db_session

        monkeypatch.setattr("backend.app.services.print_scheduler.async_session", _session_ctx)
        return PrintScheduler()

    @staticmethod
    def _idle_printer_manager():
        return patch.multiple(
            "backend.app.services.print_scheduler.printer_manager",
            is_connected=lambda pid: True,
            get_status=lambda pid: SimpleNamespace(state="IDLE", raw_data={}),
            is_awaiting_plate_clear=lambda pid: False,
        )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_gated_item_is_skipped_after_a_failure(self, db_session, printer_factory, scheduler) -> None:
        _, queue = await _printer_with_queue(db_session, printer_factory)
        await _finished(db_session, queue, "failed", minutes_ago=10)
        item = PrintQueueItem(queue_id=queue.id, status="pending", position=1, require_previous_success=True)
        db_session.add(item)
        await db_session.commit()

        sent = AsyncMock()
        with (
            patch("backend.app.services.print_scheduler.notification_service.on_queue_job_skipped", sent),
            self._idle_printer_manager(),
        ):
            await scheduler.check_queue()

        await db_session.refresh(item)
        assert item.status == "skipped"
        assert item.completed_at is not None
        assert sent.await_count == 1, "the operator has to be told the job was dropped, not just left wondering"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_ungated_item_is_untouched_by_the_same_failure(
        self, db_session, printer_factory, scheduler
    ) -> None:
        """Off by default means off: a farm that never asked for the gate keeps
        printing after a failure exactly as before."""
        _, queue = await _printer_with_queue(db_session, printer_factory)
        await _finished(db_session, queue, "failed", minutes_ago=10)
        item = PrintQueueItem(queue_id=queue.id, status="pending", position=1, require_previous_success=False)
        db_session.add(item)
        await db_session.commit()

        with self._idle_printer_manager():
            await scheduler.check_queue()

        await db_session.refresh(item)
        assert item.status != "skipped"


class TestUnskipReleasesTheGate:
    """Without this, unskipping is a no-op with extra steps: the failure the gate
    reads is still the newest one, so the next tick skips the item again."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unskipping_acknowledges_the_blocking_failure(self, db_session, printer_factory) -> None:
        printer, queue = await _printer_with_queue(db_session, printer_factory)
        failure = await _finished(db_session, queue, "failed", minutes_ago=10)

        await _acknowledge_blocking_failure(db_session, queue.id)
        await db_session.commit()

        await db_session.refresh(failure)
        assert failure.gate_acknowledged is True
        assert await PrintScheduler().previous_print_succeeded(db_session, printer.id) is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_nothing_is_acknowledged_when_the_last_print_succeeded(self, db_session, printer_factory) -> None:
        """Only the row the gate is actually reading may be retired — an older
        failure must stay on the record for anyone looking at history."""
        _, queue = await _printer_with_queue(db_session, printer_factory)
        old_failure = await _finished(db_session, queue, "failed", minutes_ago=60)
        await _finished(db_session, queue, "completed", minutes_ago=10)

        await _acknowledge_blocking_failure(db_session, queue.id)
        await db_session.commit()

        await db_session.refresh(old_failure)
        assert old_failure.gate_acknowledged is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_release_covers_every_item_behind_the_failure(self, db_session, printer_factory) -> None:
        """Acknowledgement is per-failure, not per-item: clearing it once must
        free the whole queue, or the operator unskips one job at a time."""
        printer, queue = await _printer_with_queue(db_session, printer_factory)
        await _finished(db_session, queue, "failed", minutes_ago=10)
        for pos in (1, 2, 3):
            db_session.add(
                PrintQueueItem(queue_id=queue.id, status="pending", position=pos, require_previous_success=True)
            )
        await db_session.commit()

        await _acknowledge_blocking_failure(db_session, queue.id)
        await db_session.commit()

        scheduler = PrintScheduler()
        pending = (
            (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.status == "pending"))).scalars().all()
        )
        for item in pending:
            assert await scheduler.previous_print_succeeded(db_session, printer.id, item.id) is True
