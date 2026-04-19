"""Tests for the clear plate queue flow in the print scheduler."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.print_scheduler import PrintScheduler
from backend.app.services.printer_manager import PrinterManager


class TestPrinterManagerAwaitingPlateClear:
    """Test the awaiting_plate_clear gate management in PrinterManager.

    Semantics are inverted from the previous _plate_cleared set: presence now
    means the printer is BLOCKED on user confirmation, absence means the gate
    is released.
    """

    @pytest.fixture
    def manager(self):
        mgr = PrinterManager()
        # Short-circuit the DB persist that would otherwise run as fire-and-forget
        # and interact with async_session the tests don't set up.
        mgr._persist_awaiting_plate_clear = AsyncMock(return_value=None)
        mgr._schedule_async = lambda _coro: None
        return mgr

    def test_awaiting_initially_false(self, manager):
        """No printers should be awaiting by default."""
        assert not manager.is_awaiting_plate_clear(1)
        assert not manager.is_awaiting_plate_clear(999)

    def test_arm_gate(self, manager):
        """set_awaiting_plate_clear(pid, True) arms the gate for that printer only."""
        manager.set_awaiting_plate_clear(1, True)
        assert manager.is_awaiting_plate_clear(1)
        assert not manager.is_awaiting_plate_clear(2)

    def test_release_gate(self, manager):
        """set_awaiting_plate_clear(pid, False) releases the gate."""
        manager.set_awaiting_plate_clear(1, True)
        assert manager.is_awaiting_plate_clear(1)
        manager.set_awaiting_plate_clear(1, False)
        assert not manager.is_awaiting_plate_clear(1)

    def test_release_when_not_armed_is_idempotent(self, manager):
        """Releasing a gate that was never armed is a no-op, not an error."""
        manager.set_awaiting_plate_clear(1, False)  # Should not raise
        assert not manager.is_awaiting_plate_clear(1)

    def test_gate_is_per_printer(self, manager):
        """Arming one printer doesn't affect others."""
        manager.set_awaiting_plate_clear(1, True)
        manager.set_awaiting_plate_clear(3, True)
        assert manager.is_awaiting_plate_clear(1)
        assert not manager.is_awaiting_plate_clear(2)
        assert manager.is_awaiting_plate_clear(3)

    def test_release_only_affects_target_printer(self, manager):
        """Releasing one printer's gate shouldn't release others."""
        manager.set_awaiting_plate_clear(1, True)
        manager.set_awaiting_plate_clear(2, True)
        manager.set_awaiting_plate_clear(1, False)
        assert not manager.is_awaiting_plate_clear(1)
        assert manager.is_awaiting_plate_clear(2)


class TestSchedulerIdleCheckWithAwaitingPlateClear:
    """Test _is_printer_idle with the awaiting_plate_clear gate."""

    @pytest.fixture
    def scheduler(self):
        return PrintScheduler()

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_idle_state_is_idle(self, mock_pm, scheduler):
        """Printer in IDLE state should be considered idle."""
        mock_pm.is_connected.return_value = True
        mock_pm.get_status.return_value = MagicMock(state="IDLE")
        assert scheduler._is_printer_idle(1) is True

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_running_state_not_idle(self, mock_pm, scheduler):
        """Printer in RUNNING state should not be idle."""
        mock_pm.is_connected.return_value = True
        mock_pm.get_status.return_value = MagicMock(state="RUNNING")
        assert scheduler._is_printer_idle(1) is False

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_finish_state_not_idle_when_awaiting(self, mock_pm, scheduler):
        """FINISH with gate armed blocks dispatch."""
        mock_pm.is_connected.return_value = True
        mock_pm.get_status.return_value = MagicMock(state="FINISH")
        mock_pm.is_awaiting_plate_clear.return_value = True
        assert scheduler._is_printer_idle(1) is False

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_finish_state_idle_when_gate_released(self, mock_pm, scheduler):
        """FINISH with gate released is dispatch-ready."""
        mock_pm.is_connected.return_value = True
        mock_pm.get_status.return_value = MagicMock(state="FINISH")
        mock_pm.is_awaiting_plate_clear.return_value = False
        assert scheduler._is_printer_idle(1) is True

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_failed_state_not_idle_when_awaiting(self, mock_pm, scheduler):
        """FAILED with gate armed blocks dispatch."""
        mock_pm.is_connected.return_value = True
        mock_pm.get_status.return_value = MagicMock(state="FAILED")
        mock_pm.is_awaiting_plate_clear.return_value = True
        assert scheduler._is_printer_idle(1) is False

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_failed_state_idle_when_gate_released(self, mock_pm, scheduler):
        """FAILED with gate released is dispatch-ready."""
        mock_pm.is_connected.return_value = True
        mock_pm.get_status.return_value = MagicMock(state="FAILED")
        mock_pm.is_awaiting_plate_clear.return_value = False
        assert scheduler._is_printer_idle(1) is True

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_disconnected_printer_not_idle(self, mock_pm, scheduler):
        """Disconnected printer should never be idle."""
        mock_pm.is_connected.return_value = False
        assert scheduler._is_printer_idle(1) is False

    @patch("backend.app.services.print_scheduler.printer_manager")
    def test_no_status_not_idle(self, mock_pm, scheduler):
        """Printer with no status should not be idle."""
        mock_pm.is_connected.return_value = True
        mock_pm.get_status.return_value = None
        assert scheduler._is_printer_idle(1) is False


class TestSchedulerQueueCheckLogging:
    """Test queue check logging when pending items are found (#374)."""

    @pytest.fixture
    def scheduler(self):
        return PrintScheduler()

    @pytest.mark.asyncio
    @patch("backend.app.services.print_scheduler.printer_manager")
    async def test_check_queue_logs_pending_items(self, mock_pm, scheduler, caplog):
        """Verify pending items are logged when found in check_queue."""
        mock_item = MagicMock()
        mock_item.id = 42
        mock_item.printer_id = 1
        mock_item.archive_id = 100
        mock_item.library_file_id = None
        mock_item.scheduled_time = None
        mock_item.manual_start = False
        mock_item.target_model = None

        mock_pm.is_connected.return_value = True
        mock_pm.get_status.return_value = MagicMock(state="RUNNING")

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_item]

        with (
            patch("backend.app.services.print_scheduler.async_session") as mock_session_ctx,
            caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"),
        ):
            mock_db = AsyncMock()
            mock_db.execute = AsyncMock(return_value=mock_result)
            mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            await scheduler.check_queue()

        queue_logs = [r for r in caplog.records if "Queue check" in r.message]
        assert len(queue_logs) == 1
        assert "1 pending items" in queue_logs[0].message
        assert "42" in queue_logs[0].message  # item ID

    @pytest.mark.asyncio
    async def test_check_queue_no_log_when_empty(self, scheduler, caplog):
        """Verify no queue log when no pending items found."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []

        with (
            patch("backend.app.services.print_scheduler.async_session") as mock_session_ctx,
            caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"),
        ):
            mock_db = AsyncMock()
            mock_db.execute = AsyncMock(return_value=mock_result)
            mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            await scheduler.check_queue()

        queue_logs = [r for r in caplog.records if "Queue check" in r.message]
        assert len(queue_logs) == 0
