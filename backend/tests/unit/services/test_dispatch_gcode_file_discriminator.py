"""Regression tests for the dispatch `pre_gcode_file` discriminator (#1150).

Ports upstream Bambuddy's matching test set (audit cycle v0.2.3.2 → v0.2.4b1,
item A.3). On P1P + slow SD cards, a project_file accepted by the printer
sometimes triggered a forced MQTT reconnect from our verify-watchdog while the
firmware was still parsing the file. The reconnect mid-parse caused the printer
to fail with `0500_4003 "Failed to start print job"`.

Fix: track `gcode_file` from before dispatch; when the verify watchdog times
out without seeing a state transition, only force a reconnect if `gcode_file`
is *unchanged*. A change means the project_file landed and the printer is
just slow — leave it alone (#1150). An unchanged value still indicates the
half-broken-MQTT-session pattern (#887, #936) and warrants a reconnect.

Both watchdogs are exercised:
    - `print_scheduler.PrintScheduler._watchdog_print_start`
    - `background_dispatch.BackgroundDispatchService._verify_print_response`
"""

from unittest.mock import MagicMock, patch

import pytest

from backend.app.services.background_dispatch import BackgroundDispatchService
from backend.app.services.print_scheduler import PrintScheduler


class _FakeStatus:
    def __init__(self, state: str, gcode_file: str | None = None, subtask_id: str | None = None):
        self.state = state
        self.gcode_file = gcode_file
        self.subtask_id = subtask_id


@pytest.mark.asyncio
async def test_verify_skips_reconnect_when_gcode_file_changed():
    """Slow-parse: gcode_file advanced → don't reconnect (#1150)."""
    status = _FakeStatus("FINISH", gcode_file="new.gcode")
    client = MagicMock()
    client.force_reconnect_stale_session = MagicMock()

    with (
        patch("backend.app.services.background_dispatch.printer_manager.get_status", return_value=status),
        patch("backend.app.services.background_dispatch.printer_manager.get_client", return_value=client),
    ):
        await BackgroundDispatchService._verify_print_response(
            printer_id=5,
            printer_name="P1P",
            pre_state="FINISH",
            timeout=0.05,
            poll_interval=0.01,
            pre_gcode_file="old.gcode",
        )

    client.force_reconnect_stale_session.assert_not_called()


@pytest.mark.asyncio
async def test_verify_reconnects_when_gcode_file_unchanged():
    """Half-broken session: gcode_file same as pre → reconnect (#887, #936)."""
    status = _FakeStatus("FINISH", gcode_file="same.gcode")
    client = MagicMock()
    client.force_reconnect_stale_session = MagicMock()

    with (
        patch("backend.app.services.background_dispatch.printer_manager.get_status", return_value=status),
        patch("backend.app.services.background_dispatch.printer_manager.get_client", return_value=client),
    ):
        await BackgroundDispatchService._verify_print_response(
            printer_id=5,
            printer_name="P1P",
            pre_state="FINISH",
            timeout=0.05,
            poll_interval=0.01,
            pre_gcode_file="same.gcode",
        )

    client.force_reconnect_stale_session.assert_called_once()


@pytest.mark.asyncio
async def test_verify_skips_reconnect_when_pre_was_none_and_current_is_set():
    """No pre_gcode_file but current is non-None → still treat as landed.

    Pre-state at dispatch may be missing (printer offline at command time).
    A *new* gcode_file showing up before the timeout is a strong "landed"
    signal regardless — better safe than to nuke the session.
    """
    status = _FakeStatus("FINISH", gcode_file="suddenly.gcode")
    client = MagicMock()
    client.force_reconnect_stale_session = MagicMock()

    with (
        patch("backend.app.services.background_dispatch.printer_manager.get_status", return_value=status),
        patch("backend.app.services.background_dispatch.printer_manager.get_client", return_value=client),
    ):
        await BackgroundDispatchService._verify_print_response(
            printer_id=5,
            printer_name="P1P",
            pre_state="FINISH",
            timeout=0.05,
            poll_interval=0.01,
            pre_gcode_file=None,
        )

    client.force_reconnect_stale_session.assert_not_called()


@pytest.mark.asyncio
async def test_verify_reconnects_when_pre_gcode_file_omitted_default():
    """Backward compat: omitted arg defaults to None → unchanged → reconnect."""
    status = _FakeStatus("FINISH", gcode_file=None)
    client = MagicMock()
    client.force_reconnect_stale_session = MagicMock()

    with (
        patch("backend.app.services.background_dispatch.printer_manager.get_status", return_value=status),
        patch("backend.app.services.background_dispatch.printer_manager.get_client", return_value=client),
    ):
        await BackgroundDispatchService._verify_print_response(
            printer_id=5,
            printer_name="P1P",
            pre_state="FINISH",
            timeout=0.05,
            poll_interval=0.01,
        )

    client.force_reconnect_stale_session.assert_called_once()


# --- Scheduler-side watchdog ------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_watchdog_skips_reconnect_when_gcode_file_changed():
    """Same #1150 logic on the scheduler-side watchdog.

    Uses ``swap_start_fired=True`` to keep the watchdog on the no-DB-revert
    branch — we only want to exercise the discriminator at the bottom.
    """
    status = _FakeStatus("FINISH", gcode_file="new.gcode")
    client = MagicMock()
    client.force_reconnect_stale_session = MagicMock()

    with (
        patch("backend.app.services.print_scheduler.printer_manager.get_status", return_value=status),
        patch("backend.app.services.print_scheduler.printer_manager.get_client", return_value=client),
    ):
        await PrintScheduler._watchdog_print_start(
            queue_item_id=1,
            printer_id=5,
            pre_state="FINISH",
            pre_subtask_id=None,
            swap_start_fired=True,
            timeout=0.05,
            poll_interval=0.01,
            pre_gcode_file="old.gcode",
        )

    client.force_reconnect_stale_session.assert_not_called()


@pytest.mark.asyncio
async def test_scheduler_watchdog_reconnects_when_gcode_file_unchanged():
    status = _FakeStatus("FINISH", gcode_file="same.gcode")
    client = MagicMock()
    client.force_reconnect_stale_session = MagicMock()

    with (
        patch("backend.app.services.print_scheduler.printer_manager.get_status", return_value=status),
        patch("backend.app.services.print_scheduler.printer_manager.get_client", return_value=client),
    ):
        await PrintScheduler._watchdog_print_start(
            queue_item_id=1,
            printer_id=5,
            pre_state="FINISH",
            pre_subtask_id=None,
            swap_start_fired=True,
            timeout=0.05,
            poll_interval=0.01,
            pre_gcode_file="same.gcode",
        )

    client.force_reconnect_stale_session.assert_called_once()
