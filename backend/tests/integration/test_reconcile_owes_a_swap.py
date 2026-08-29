"""A swap the dead process still owed is settled, not skipped.

Measured 2026-08-29: four swap minis finished overnight while the PC was
off. The sweep closed their archives, nobody ran ``swap_mode_change_table``
(the pending checklist on each archive still said so), and — swap printers
having ``require_plate_clear=False`` — the armed plate gate held nothing:
new jobs dispatched onto un-swapped tables a minute later.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

import backend.app.models.printer_location  # noqa: F401 — resolves Printer's rel
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.print_reconciliation import _reconcile_complete_archive, _resolve_pending_swaps

pytestmark = pytest.mark.integration


def _global_session_on(db_session):
    """``_resolve_pending_swaps`` opens its own sessions via the module-level
    factory, which only the ``async_client`` fixture rebinds — point it at the
    test engine for these direct-call tests."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    return patch("backend.app.core.database.async_session", maker)


async def _swap_orphan(db, printer_factory, *, uncertain=False, swap_enabled=True):
    printer = await printer_factory(swap_mode_enabled=swap_enabled, require_plate_clear=False)
    queue = PrinterQueue(printer_id=printer.id, status="printing")
    db.add(queue)
    archive = PrintArchive(
        printer_id=printer.id,
        filename="job.gcode.3mf",
        print_name="Overnight_Swap_Job",
        file_path="x/job.gcode.3mf",
        file_size=1,
        status="printing",
        started_at=datetime.now(timezone.utc),
        extra_data={"swap_macro_events_pending": ["swap_mode_change_table"]},
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    await db.refresh(queue)
    return printer, queue, archive


async def _close(db, archive, *, uncertain=False):
    with patch("backend.app.services.usage_tracker.on_print_complete", new_callable=AsyncMock):
        await _reconcile_complete_archive(db, archive, status="completed", uncertain=uncertain)
    await db.commit()


class TestTheCloseKeepsTheClaim:
    @pytest.mark.asyncio
    async def test_queue_claim_survives_while_the_swap_is_owed(self, db_session, printer_factory):
        printer, queue, archive = await _swap_orphan(db_session, printer_factory)
        item = PrintQueueItem(queue_id=queue.id, status="printing", position=0, archive_id=archive.id)
        db_session.add(item)
        await db_session.commit()

        await _close(db_session, archive)

        await db_session.refresh(queue)
        assert queue.status == "printing", "the claim is the block — releasing it re-opens the 12:04 race"

    @pytest.mark.asyncio
    async def test_plate_gate_is_not_armed_while_the_swap_is_owed(self, db_session, printer_factory):
        printer, queue, archive = await _swap_orphan(db_session, printer_factory)

        with patch("backend.app.services.printer_manager.printer_manager.set_awaiting_plate_clear") as mock_arm:
            await _close(db_session, archive)

        for call in mock_arm.call_args_list:
            assert call.args != (printer.id, True), "swap resolution owns the outcome, not the manual gate"

    @pytest.mark.asyncio
    async def test_a_non_swap_archive_still_releases_and_arms(self, db_session, printer_factory):
        printer = await printer_factory(require_plate_clear=True)
        queue = PrinterQueue(printer_id=printer.id, status="printing")
        db_session.add(queue)
        archive = PrintArchive(
            printer_id=printer.id,
            filename="j.gcode.3mf",
            file_path="x/j.gcode.3mf",
            file_size=1,
            status="printing",
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(archive)
        await db_session.commit()
        await db_session.refresh(archive)

        with patch("backend.app.services.printer_manager.printer_manager.set_awaiting_plate_clear") as mock_arm:
            await _close(db_session, archive)

        mock_arm.assert_any_call(printer.id, True)


class TestTheResolution:
    @pytest.mark.asyncio
    async def test_certain_completion_runs_the_macro_and_releases(self, db_session, printer_factory):
        printer, queue, archive = await _swap_orphan(db_session, printer_factory)
        await _close(db_session, archive)

        macro = type("M", (), {"name": "change", "gcode": "G28"})()
        with (
            patch("backend.app.services.macro_executor.find_swap_macro", new_callable=AsyncMock, return_value=macro),
            patch(
                "backend.app.services.printer_manager.printer_manager.execute_macro_and_wait",
                new_callable=AsyncMock,
                return_value=(True, "ok"),
            ) as mock_exec,
            _global_session_on(db_session),
        ):
            await _resolve_pending_swaps(printer.id, [archive.id])

        mock_exec.assert_awaited_once()
        await db_session.refresh(queue)
        assert queue.status == "idle"
        fresh = (
            await db_session.execute(select(PrintArchive.extra_data).where(PrintArchive.id == archive.id))
        ).scalar_one()
        assert "swap_mode_change_table" not in (fresh.get("swap_macro_events_pending") or [])

    @pytest.mark.asyncio
    async def test_uncertain_outcome_pauses_instead_of_swapping(self, db_session, printer_factory):
        printer, queue, archive = await _swap_orphan(db_session, printer_factory)
        item = PrintQueueItem(queue_id=queue.id, status="pending", position=1)
        db_session.add(item)
        await db_session.commit()
        await _close(db_session, archive, uncertain=True)

        with (
            patch(
                "backend.app.services.printer_manager.printer_manager.execute_macro_and_wait",
                new_callable=AsyncMock,
            ) as mock_exec,
            _global_session_on(db_session),
        ):
            await _resolve_pending_swaps(printer.id, [archive.id])

        mock_exec.assert_not_awaited()
        await db_session.refresh(queue)
        await db_session.refresh(item)
        assert queue.status == "paused"
        assert "uncertain" in (item.waiting_reason or "")

    @pytest.mark.asyncio
    async def test_a_failed_macro_pauses_with_the_reason(self, db_session, printer_factory):
        printer, queue, archive = await _swap_orphan(db_session, printer_factory)
        item = PrintQueueItem(queue_id=queue.id, status="pending", position=1)
        db_session.add(item)
        await db_session.commit()
        await _close(db_session, archive)

        macro = type("M", (), {"name": "change", "gcode": "G28"})()
        with (
            patch("backend.app.services.macro_executor.find_swap_macro", new_callable=AsyncMock, return_value=macro),
            patch(
                "backend.app.services.printer_manager.printer_manager.execute_macro_and_wait",
                new_callable=AsyncMock,
                return_value=(False, "no ACK"),
            ),
            _global_session_on(db_session),
        ):
            await _resolve_pending_swaps(printer.id, [archive.id])

        await db_session.refresh(queue)
        await db_session.refresh(item)
        assert queue.status == "paused"
        assert "no ACK" in (item.waiting_reason or "")


def test_the_live_failure_path_actually_pauses():
    """The old code set waiting_reason and called it a pause — the scheduler
    recomputes that field every tick, so nothing ever held. Pin the real
    pause in the live change_table failure branch."""
    import inspect

    from backend.app import main as main_mod

    source = inspect.getsource(main_mod.on_print_complete)
    failure_branch = source[source.index("change_table macro failed") :]
    assert "set_queue_paused" in failure_branch[:2000]
