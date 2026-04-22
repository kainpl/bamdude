"""Unit tests for ``PrintScheduler._watchdog_print_start``.

Regression coverage for upstream #1078: H2D Pro firmware (01.01.00.00)
keeps ``gcode_state=FINISH`` for 48–55 s after accepting a ``project_file``
command before transitioning to PREPARE. The old watchdog reverted items
the printer had already started physically printing, and the next
scheduler tick re-dispatched them — looked like a reprint.

The fix adds a second "command landed" signal: ``subtask_id`` advancing
past the pre-dispatch value. The printer echoes the submission_id we
minted (``bambu_mqtt._publish_project``, upstream #1042) in its next
``push_status.subtask_id``, well before the state transitions on slow
firmware. Timeout also bumped 45 → 90 s as belt-and-braces.

These tests use short `timeout=0.3` / `poll_interval=0.05` values to
keep the suite fast while still exercising the full poll loop.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.print_scheduler import PrintScheduler


def _make_status(state: str = "FINISH", subtask_id: str | None = None):
    status = MagicMock()
    status.state = state
    status.subtask_id = subtask_id
    return status


class TestWatchdogGating:
    """State / subtask_id exit conditions — the core of the fix."""

    @pytest.mark.asyncio
    async def test_state_change_exits_early(self):
        """Classic signal: printer transitions out of pre_state → no revert."""
        # First poll shows RUNNING (state changed from FINISH), watchdog returns.
        with (
            patch("backend.app.services.print_scheduler.printer_manager") as pm,
            patch("backend.app.services.print_scheduler.async_session") as session,
        ):
            pm.get_status.return_value = _make_status(state="RUNNING", subtask_id="old")
            pm.get_client.return_value = None

            await PrintScheduler._watchdog_print_start(
                queue_item_id=1,
                printer_id=1,
                pre_state="FINISH",
                pre_subtask_id="old",
                swap_start_fired=False,
                timeout=0.3,
                poll_interval=0.05,
            )

            # Watchdog exited early — no DB session should have been opened.
            session.assert_not_called()

    @pytest.mark.asyncio
    async def test_subtask_advance_exits_early_even_when_state_stuck(self):
        """New signal (upstream #1078): state stays FINISH but subtask_id advances.

        This is the H2D Pro case: state lags the accepted command by up to
        55 s, but subtask_id flips back in the first push_status.
        """
        with (
            patch("backend.app.services.print_scheduler.printer_manager") as pm,
            patch("backend.app.services.print_scheduler.async_session") as session,
        ):
            pm.get_status.return_value = _make_status(state="FINISH", subtask_id="new-submission-id")

            await PrintScheduler._watchdog_print_start(
                queue_item_id=1,
                printer_id=1,
                pre_state="FINISH",
                pre_subtask_id="prior-submission-id",
                swap_start_fired=False,
                timeout=0.3,
                poll_interval=0.05,
            )

            # subtask_id advanced → watchdog treats the command as landed.
            session.assert_not_called()

    @pytest.mark.asyncio
    async def test_both_signals_unchanged_triggers_revert(self):
        """Genuinely half-broken session (pre-fix behaviour preserved): both
        signals unchanged across the full timeout → revert the queue item.
        """
        pre_state = "FINISH"
        pre_subtask = "sid-1"

        mock_item = MagicMock()
        mock_item.status = "printing"

        mock_db = AsyncMock()
        mock_db.get.return_value = mock_item
        mock_db.commit = AsyncMock()

        # async_session() returns an async-context-manager yielding mock_db
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_db
        mock_ctx.__aexit__.return_value = False

        with (
            patch("backend.app.services.print_scheduler.printer_manager") as pm,
            patch("backend.app.services.print_scheduler.async_session", return_value=mock_ctx),
        ):
            pm.get_status.return_value = _make_status(state=pre_state, subtask_id=pre_subtask)
            pm.get_client.return_value = None

            await PrintScheduler._watchdog_print_start(
                queue_item_id=1,
                printer_id=1,
                pre_state=pre_state,
                pre_subtask_id=pre_subtask,
                swap_start_fired=False,
                timeout=0.15,
                poll_interval=0.05,
            )

            # The queue item was flipped back to 'pending' and started_at cleared.
            assert mock_item.status == "pending"
            assert mock_item.started_at is None
            mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_swap_fired_never_reverts(self):
        """swap_start_fired=True keeps item in 'printing' even on full timeout
        — reverting would re-fire swap_mode_start on the next dispatch and cause
        a double physical table swap. Pre-existing BamDude guard.
        """
        mock_db_get = AsyncMock()

        with (
            patch("backend.app.services.print_scheduler.printer_manager") as pm,
            patch("backend.app.services.print_scheduler.async_session") as session,
        ):
            pm.get_status.return_value = _make_status(state="FINISH", subtask_id="sid")
            pm.get_client.return_value = None

            await PrintScheduler._watchdog_print_start(
                queue_item_id=1,
                printer_id=1,
                pre_state="FINISH",
                pre_subtask_id="sid",
                swap_start_fired=True,
                timeout=0.15,
                poll_interval=0.05,
            )

            # Swap guard: must NOT open a DB session to revert.
            session.assert_not_called()
            mock_db_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_subtask_id_baseline_falls_back_to_state_only(self):
        """When pre_subtask_id is None (printer hadn't emitted one yet), the
        new guard must not short-circuit on a None→"any" transition. Only
        state-change should count in that case.
        """
        mock_item = MagicMock()
        mock_item.status = "printing"

        mock_db = AsyncMock()
        mock_db.get.return_value = mock_item
        mock_db.commit = AsyncMock()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_db
        mock_ctx.__aexit__.return_value = False

        with (
            patch("backend.app.services.print_scheduler.printer_manager") as pm,
            patch("backend.app.services.print_scheduler.async_session", return_value=mock_ctx),
        ):
            # state unchanged; subtask transitions None → "first-ever-id" — MUST
            # NOT be mistaken as advance since pre_subtask_id was None.
            pm.get_status.return_value = _make_status(state="FINISH", subtask_id="first-ever-id")
            pm.get_client.return_value = None

            await PrintScheduler._watchdog_print_start(
                queue_item_id=1,
                printer_id=1,
                pre_state="FINISH",
                pre_subtask_id=None,
                swap_start_fired=False,
                timeout=0.15,
                poll_interval=0.05,
            )

            # Revert should have fired — state never changed, None pre_subtask
            # disables the subtask-advance guard.
            assert mock_item.status == "pending"
