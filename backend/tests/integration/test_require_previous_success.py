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
from backend.tests.integration.swap_terminal_support import an_execution, complete, item_of, make_rig  # noqa: F401

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

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_row_behind_a_head_that_waits_is_not_judged(self, db_session, printer_factory, scheduler) -> None:
        """G03 E2: only the printer's next row meets its gate.

        The head waits for its filament; the gated row behind it is not next — the
        claim would refuse it — so it must not be skipped for a failure the head's
        own print may clear first.
        """
        from backend.app.services.filament_routing import RoutingDeferred

        _, queue = await _printer_with_queue(db_session, printer_factory)
        await _finished(db_session, queue, "failed", minutes_ago=10)
        head = PrintQueueItem(queue_id=queue.id, status="pending", position=1, require_previous_success=False)
        behind = PrintQueueItem(queue_id=queue.id, status="pending", position=2, require_previous_success=True)
        db_session.add_all([head, behind])
        await db_session.commit()

        skipped = AsyncMock()
        with (
            patch("backend.app.services.print_scheduler.notification_service.on_queue_job_skipped", skipped),
            patch(
                "backend.app.services.print_scheduler.preflight_item",
                AsyncMock(side_effect=RoutingDeferred("material_mismatch")),
            ) as preflight,
            self._idle_printer_manager(),
        ):
            await scheduler.check_queue()

        await db_session.refresh(head)
        await db_session.refresh(behind)
        assert head.waiting_reason_code == "filament_unavailable"
        assert behind.status == "pending", "a row that is not next must not be skipped by its gate"
        assert skipped.await_count == 0
        assert preflight.await_count == 1, "only the head is preflighted"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_head_skipped_by_its_own_gate_leaves_the_next_row_judged(
        self, db_session, printer_factory, scheduler
    ) -> None:
        """A skipped head is no longer next, so the row behind it is judged in the same pass (as before)."""
        _, queue = await _printer_with_queue(db_session, printer_factory)
        await _finished(db_session, queue, "failed", minutes_ago=10)
        rows = [
            PrintQueueItem(queue_id=queue.id, status="pending", position=pos, require_previous_success=True)
            for pos in (1, 2)
        ]
        db_session.add_all(rows)
        await db_session.commit()

        skipped = AsyncMock()
        with (
            patch("backend.app.services.print_scheduler.notification_service.on_queue_job_skipped", skipped),
            self._idle_printer_manager(),
        ):
            await scheduler.check_queue()

        for row in rows:
            await db_session.refresh(row)
        assert [row.status for row in rows] == ["skipped", "skipped"]
        assert skipped.await_count == 2


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


class TestTheGateSeesPrintsItUsedToMiss:
    """⚠️ A behaviour change, taken on purpose — and narrowed by the owner (G03 O-3).

    The gate asks "did the last print on this machine fail". Before every print
    held a queue row it could only see prints the queue tier dispatched, so a
    failed **Print now** — or a failed print started on the printer's screen —
    gated nothing queued behind it, on the same machine, minutes later.

    What it reads is a PRINT that failed: a start the printer was sent and did
    not take still gates; an attempt that never sent a start (a refused upload,
    a printer that dropped off before the command) failed no print and does not.
    """

    @staticmethod
    async def _released_direct_attempt(db_session, printer, monkeypatch, source, capture, *, send_fact):
        """The real claim and the real release, because the point is the row those two leave behind."""
        from contextlib import asynccontextmanager

        from backend.app.services.background_dispatch import PrintDispatchJob, background_dispatch
        from backend.app.services.queue_batch import claim_printer_for_direct_print

        claimed = await claim_printer_for_direct_print(
            db_session,
            printer_id=printer.id,
            origin="direct",
            library_file_id=source.id,
            staged=await capture(source),
        )

        @asynccontextmanager
        async def _session_ctx():
            yield db_session

        monkeypatch.setattr("backend.app.services.background_dispatch.async_session", _session_ctx)
        job = PrintDispatchJob(
            id=1,
            kind="print_library_file",
            source_id=1,
            source_name="file1.gcode.3mf",
            printer_id=printer.id,
            printer_name=printer.name,
            queue_item_id=claimed.id,
            claim_started_at=claimed.started_at,
        )
        job.send_fact = send_fact
        return job, background_dispatch

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_direct_print_that_never_sent_its_start_gates_nothing(
        self, db_session, printer_factory, monkeypatch, raw_gcode_source, a_direct_capture
    ) -> None:
        """An upload the printer refused ("FTP 553") reached no print: the next job runs."""
        printer, _queue = await _printer_with_queue(db_session, printer_factory)
        job, dispatch = await self._released_direct_attempt(
            db_session, printer, monkeypatch, raw_gcode_source, a_direct_capture, send_fact=None
        )
        job.outcome = {"success": False, "archive_id": None, "error": "FTP 553", "cancelled": False}
        await dispatch._release_direct_claim(job, status="failed")

        row = await db_session.get(PrintQueueItem, job.queue_item_id, populate_existing=True)
        assert (row.status, row.gate_acknowledged) == ("failed", True)
        assert await PrintScheduler().previous_print_succeeded(db_session, printer.id) is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_direct_print_the_printer_did_not_start_still_gates(
        self, db_session, printer_factory, monkeypatch, raw_gcode_source, a_direct_capture
    ) -> None:
        """The start was published and the printer's verdict was "not started": that is a print that failed."""
        from backend.app.services.background_dispatch import SEND_SENT

        printer, _queue = await _printer_with_queue(db_session, printer_factory)
        job, dispatch = await self._released_direct_attempt(
            db_session, printer, monkeypatch, raw_gcode_source, a_direct_capture, send_fact=SEND_SENT
        )
        job.outcome = {"success": False, "archive_id": None, "error": "Printer did not start", "cancelled": False}
        await dispatch._release_direct_claim(job, status="failed")

        row = await db_session.get(PrintQueueItem, job.queue_item_id, populate_existing=True)
        assert (row.status, row.gate_acknowledged) == ("failed", False)
        assert await PrintScheduler().previous_print_succeeded(db_session, printer.id) is False


class TestTheLookbackOrder:
    """One reader for both tiers, ``completed_at DESC NULLS LAST, id DESC`` (G03 E3a, upstream 5c575b5ba)."""

    def test_the_order_states_its_null_rule_and_ends_with_the_id(self) -> None:
        """PostgreSQL sorts NULL FIRST in a bare ``DESC``: one cancel left without an end time hid every later
        failure there. Pinned on the compiled SQL, because SQLite sorts NULL last and cannot show it."""
        from sqlalchemy.dialects import postgresql

        from backend.app.services.previous_print_gate import _newest_first

        sql = str(select(PrintQueueItem.id).order_by(*_newest_first()).compile(dialect=postgresql.dialect()))
        assert "completed_at DESC NULLS LAST, print_queue.id DESC" in sql

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_cancel_without_an_end_time_does_not_hide_a_later_failure(
        self, db_session, printer_factory
    ) -> None:
        printer, queue = await _printer_with_queue(db_session, printer_factory)
        db_session.add(PrintQueueItem(queue_id=queue.id, status="cancelled", position=0, completed_at=None))
        await db_session.commit()
        await _finished(db_session, queue, "failed", minutes_ago=5)
        assert await PrintScheduler().previous_print_succeeded(db_session, printer.id) is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.parametrize("newest", ["failed", "completed"])
    async def test_rows_that_ended_at_the_same_moment_are_told_apart_by_id(
        self, db_session, printer_factory, newest
    ) -> None:
        printer, queue = await _printer_with_queue(db_session, printer_factory)
        await _finished(db_session, queue, "completed" if newest == "failed" else "failed", minutes_ago=5)
        await _finished(db_session, queue, newest, minutes_ago=5)
        assert await PrintScheduler().previous_print_succeeded(db_session, printer.id) is (newest != "failed")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_list_flag_reads_the_row_the_gate_reads(self, db_session, printer_factory) -> None:
        from backend.app.services.previous_print_gate import blocking_failure_queue_ids, failure_blocks

        _, failed = await _printer_with_queue(db_session, printer_factory, name="failed")
        _, recovered = await _printer_with_queue(db_session, printer_factory, name="recovered")
        _, acknowledged = await _printer_with_queue(db_session, printer_factory, name="acknowledged")
        _, fresh = await _printer_with_queue(db_session, printer_factory, name="fresh")
        await _finished(db_session, failed, "failed", minutes_ago=5)
        await _finished(db_session, recovered, "failed", minutes_ago=10)
        await _finished(db_session, recovered, "completed", minutes_ago=5)
        await _finished(db_session, acknowledged, "failed", minutes_ago=5, gate_acknowledged=True)

        queues = [failed.id, recovered.id, acknowledged.id, fresh.id]
        assert await blocking_failure_queue_ids(db_session, queues) == {failed.id}
        assert [await failure_blocks(db_session, queue_id) for queue_id in queues] == [True, False, False, False]


class TestASuccessReleasesTheGate:
    """G03 D1(a): a success releases the gate by itself -- durably, although its row is deleted.

    End to end through the real writers and the real clean-up: the success row is never built by hand, and it is
    gone when the gate is read. Before, the deleted success left the old failure as the newest row, so one
    unretried failure kept the gate shut for ever, on both tiers.
    """

    @staticmethod
    async def _old_failure(rig) -> int:
        async with rig.db() as db:
            failure = PrintQueueItem(
                queue_id=rig.queue_id,
                status="failed",
                position=0,
                completed_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
            db.add(failure)
            await db.commit()
            return failure.id

    @staticmethod
    async def _gate(rig, failure_id) -> tuple[bool, bool]:
        async with rig.db() as db:
            failure = await db.get(PrintQueueItem, failure_id)
            return failure.gate_acknowledged, await PrintScheduler().previous_print_succeeded(db, rig.printer_id)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_live_completion_releases_it_although_its_row_is_cleaned_up(self, make_rig) -> None:
        rig = await make_rig(require_plate_clear=False, swap_mode=False)
        failure_id = await self._old_failure(rig)
        async with rig.db() as db:
            assert await PrintScheduler().previous_print_succeeded(db, rig.printer_id) is False

        _archive_id, item_id = await an_execution(rig, fact=False)
        await complete(rig)

        assert await item_of(rig, item_id) is None, "the success row is cleaned up at completion"
        assert await self._gate(rig, failure_id) == (True, True)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_clear_plate_dropping_the_held_row_keeps_it_released(self, make_rig) -> None:
        from backend.app.services.plate_hold import answer_by_clearing

        rig = await make_rig(require_plate_clear=True, swap_mode=False)
        failure_id = await self._old_failure(rig)
        _archive_id, item_id = await an_execution(rig, fact=False)
        await complete(rig)
        assert (await item_of(rig, item_id)).status == "completed", "held for the plate answer"

        async with rig.db() as db:
            assert await answer_by_clearing(db, rig.printer_id) == 1
        assert await item_of(rig, item_id) is None
        assert await self._gate(rig, failure_id) == (True, True)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_sweep_close_releases_it_too(self, make_rig) -> None:
        from backend.app.models.archive import PrintArchive
        from backend.app.services.print_reconciliation import _reconcile_complete_archive

        rig = await make_rig(require_plate_clear=False, swap_mode=False)
        failure_id = await self._old_failure(rig)
        archive_id, item_id = await an_execution(rig, fact=False, bound=False)

        async with rig.db() as db:
            archive = await db.get(PrintArchive, archive_id)
            assert await _reconcile_complete_archive(db, archive, status="completed", uncertain=False) == archive_id
            await db.commit()

        assert await item_of(rig, item_id) is None, "the sweep cleans the row up as the live handler does"
        assert await self._gate(rig, failure_id) == (True, True)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_failed_completion_acknowledges_nothing(self, make_rig) -> None:
        rig = await make_rig(require_plate_clear=False, swap_mode=False)
        failure_id = await self._old_failure(rig)
        await an_execution(rig, fact=False)
        await complete(rig, {"status": "failed", "subtask_id": "81"})

        assert await self._gate(rig, failure_id) == (False, False)


class TestAnAttemptThatSentNothingGatesNothing:
    """G03 O-3 on the queue tier: only a start the printer was sent can fail a print."""

    @pytest.fixture
    def scheduler(self, monkeypatch, db_session):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _session_ctx():
            yield db_session

        monkeypatch.setattr("backend.app.services.print_scheduler.async_session", _session_ctx)
        return PrintScheduler()

    @staticmethod
    async def _claimed(db_session, queue) -> PrintQueueItem:
        item = PrintQueueItem(queue_id=queue.id, status="printing", position=1, started_at=datetime.now(timezone.utc))
        db_session.add(item)
        await db_session.flush()
        queue.status, queue.current_item_id = "printing", item.id
        await db_session.commit()
        await db_session.refresh(item)
        return item

    @staticmethod
    async def _finalize(scheduler, printer, item, *, send_fact):
        outcome = {
            "success": False,
            "error": "The upload did not complete",
            "failure_code": "dispatch_upload_failed",
            "send_fact": send_fact,
            "claim_started_at": item.started_at,
        }
        with (
            patch(
                "backend.app.services.background_dispatch.background_dispatch.run_from_queue_item",
                AsyncMock(return_value=outcome),
            ),
            patch("backend.app.services.print_scheduler.notification_service.on_queue_job_failed", AsyncMock()),
        ):
            await scheduler._dispatch_and_finalize(
                queue_item_id=item.id,
                printer_id=printer.id,
                printer_name=printer.name,
                printer_serial=printer.serial_number,
                dispatch_kind="print_library_file",
                dispatch_source_id=None,
                dispatch_source_name="job.gcode.3mf",
                options={},
                requested_by_user_id=None,
                project_id=None,
                project_line_id=None,
                job_name_short="job",
                swap_events=[],
                claim_started_at=item.started_at,
            )

    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.parametrize(("send_fact", "gates"), [(None, False), ("refused", False), ("sent", True)])
    async def test_the_failed_row_gates_only_after_a_published_start(
        self, db_session, printer_factory, scheduler, send_fact, gates
    ) -> None:
        """... and a start that went nowhere gives its stagger slot back at once (G03 E-10)."""
        printer, queue = await _printer_with_queue(db_session, printer_factory)
        item = await self._claimed(db_session, queue)
        scheduler._register_stagger_start(printer.id, 600)

        with patch("backend.app.services.print_scheduler.ws_manager.send_stagger_changed", AsyncMock()):
            await self._finalize(scheduler, printer, item, send_fact=send_fact)

        row = await db_session.get(PrintQueueItem, item.id, populate_existing=True)
        assert row.status == "failed"
        assert row.gate_acknowledged is (not gates)
        assert await scheduler.previous_print_succeeded(db_session, printer.id) is (not gates)
        holds_slot = any(slot.printer_id == printer.id for slot in scheduler._stagger_slots)
        assert holds_slot is gates, (
            "a start that may have reached the printer keeps its slot; one that did not frees it"
        )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_refusal_before_the_claim_gates_nothing(self, db_session, printer_factory, scheduler) -> None:
        """``_fail_item``: printer missing / offline / a nozzle mismatch -- nothing was ever sent."""
        from backend.app.utils import failure_reasons

        printer, queue = await _printer_with_queue(db_session, printer_factory)
        item = PrintQueueItem(queue_id=queue.id, status="pending", position=1)
        db_session.add(item)
        await db_session.commit()
        await scheduler._fail_item(
            db_session, item, "Printer not connected", failure_reasons.DISPATCH_PRINTER_UNAVAILABLE
        )

        await db_session.refresh(item)
        assert (item.status, item.gate_acknowledged) == ("failed", True)
        assert await scheduler.previous_print_succeeded(db_session, printer.id) is True


class TestAcknowledgeFailure:
    """G03 D4: an AutoQueue job waiting on ``previous_print_failed`` has no skipped row to unskip."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_route_acknowledges_the_failure_the_gate_reads(
        self, async_client, db_session, printer_factory
    ) -> None:
        printer, queue = await _printer_with_queue(db_session, printer_factory)
        failure = await _finished(db_session, queue, "failed", minutes_ago=5)
        printer_id, queue_id, failure_id = printer.id, queue.id, failure.id

        listed = await async_client.get("/api/v1/queues/")
        assert listed.status_code == 200, listed.text
        assert next(q for q in listed.json() if q["id"] == queue_id)["previous_print_failed"] is True
        single = await async_client.get(f"/api/v1/queues/{queue_id}")
        assert single.json()["previous_print_failed"] is True

        response = await async_client.post(f"/api/v1/queues/{queue_id}/acknowledge-failure")
        assert response.status_code == 200, response.text
        assert response.json() == {"acknowledged": True}

        db_session.expire_all()
        assert (await db_session.get(PrintQueueItem, failure_id)).gate_acknowledged is True
        assert await PrintScheduler().previous_print_succeeded(db_session, printer_id) is True
        single = await async_client.get(f"/api/v1/queues/{queue_id}")
        assert single.json()["previous_print_failed"] is False

        again = await async_client.post(f"/api/v1/queues/{queue_id}/acknowledge-failure")
        assert again.json() == {"acknowledged": False}, "nothing is gating any more"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_missing_queue_is_404(self, async_client) -> None:
        response = await async_client.post("/api/v1/queues/987654/acknowledge-failure")
        assert response.status_code == 404
