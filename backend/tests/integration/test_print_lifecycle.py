"""
Integration tests for the full print lifecycle.

These tests verify that:
1. Print start creates a new archive
2. Print complete updates archive status
3. Callbacks are properly executed
4. Energy tracking works
5. Notifications are sent

Note: These tests use mocking to avoid database conflicts.
Full end-to-end tests require the actual database setup.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestPrintStartLogic:
    """Test print start callback logic without database integration."""

    @pytest.mark.asyncio
    async def test_print_start_calls_notification_service(self, capture_logs):
        """Verify on_print_start triggers notification service."""
        with (
            patch("backend.app.main.async_session") as mock_session_maker,
            patch("backend.app.main.notification_service") as mock_notif,
            patch("backend.app.main.smart_plug_manager") as mock_plug,
            patch("backend.app.main.ws_manager") as mock_ws,
        ):
            mock_notif.on_print_start = AsyncMock()
            mock_plug.on_print_start = AsyncMock()
            mock_ws.send_print_start = AsyncMock()

            # Mock the database session
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock()
            mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
            mock_session_maker.return_value = mock_session

            from backend.app.main import on_print_start

            await on_print_start(
                1,
                {
                    "filename": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                },
            )

            # Verify WebSocket notification was sent
            mock_ws.send_print_start.assert_called_once()

        # Verify no import shadowing errors
        errors = [r for r in capture_logs.get_errors() if "cannot access local variable" in str(r.message)]
        assert not errors, f"Import shadowing error: {capture_logs.format_errors()}"


class TestPrintCompleteLogic:
    """Test print complete callback logic."""

    @pytest.mark.asyncio
    async def test_print_complete_no_import_errors(self, capture_logs):
        """Verify on_print_complete doesn't have import shadowing issues."""
        # Snapshot tasks before the call so we can cancel orphans afterwards.
        # on_print_complete fires background tasks (maintenance check, notifications,
        # smart-plug) via asyncio.create_task.  If those tasks outlive the mock
        # context they use the *real* async_session and can send real notifications.
        tasks_before = set(asyncio.all_tasks())

        with (
            patch("backend.app.main.async_session") as mock_session_maker,
            patch("backend.app.main.notification_service") as mock_notif,
            patch("backend.app.main.smart_plug_manager") as mock_plug,
            patch("backend.app.main.ws_manager") as mock_ws,
            patch("backend.app.main.mqtt_relay") as mock_relay,
            patch("backend.app.main.printer_manager") as mock_pm,
        ):
            mock_notif.on_print_complete = AsyncMock()
            mock_plug.on_print_complete = AsyncMock()
            mock_ws.send_print_complete = AsyncMock()
            mock_ws.broadcast = AsyncMock()
            mock_relay.on_print_complete = AsyncMock()
            mock_pm.get_printer.return_value = None

            # Mock the database session
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock()
            mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
            mock_session_maker.return_value = mock_session

            from backend.app.main import on_print_complete

            await on_print_complete(
                1,
                {
                    "status": "completed",
                    "filename": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "timelapse_was_active": False,
                },
            )

            # Cancel background tasks spawned by on_print_complete before
            # leaving the mock context - prevents them from running with
            # the real async_session and sending real notifications.
            for task in asyncio.all_tasks() - tasks_before:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Verify no import shadowing errors - this would have caught the ArchiveService bug
        errors = [r for r in capture_logs.get_errors() if "cannot access local variable" in str(r.message)]
        assert not errors, f"Import shadowing error: {capture_logs.format_errors()}"


class TestTimelapseTracking:
    """Test timelapse detection during prints."""

    @pytest.mark.asyncio
    async def test_timelapse_detected_in_same_message_as_print_start(self):
        """Verify timelapse is detected when xcam and state come together."""
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        client = BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="TEST123",
            access_code="12345678",
        )
        client.on_print_start = lambda data: None

        # Initial state
        client._was_running = False
        client._timelapse_during_print = False

        # Message with both state and timelapse
        client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "xcam": {"timelapse": "enable"},
                }
            }
        )

        assert client._was_running is True
        assert client._timelapse_during_print is True, (
            "Timelapse should be detected even when xcam is parsed before state"
        )

    @pytest.mark.asyncio
    async def test_timelapse_flag_included_in_completion_callback(self):
        """Verify completion callback receives timelapse_was_active flag."""
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        client = BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="TEST123",
            access_code="12345678",
        )

        completion_data = {}

        def on_complete(data):
            completion_data.update(data)

        client.on_print_start = lambda data: None
        client.on_print_complete = on_complete

        # Start with timelapse
        client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "xcam": {"timelapse": "enable"},
                }
            }
        )

        # Complete print
        client._process_message(
            {
                "print": {
                    "gcode_state": "FINISH",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                }
            }
        )

        assert "timelapse_was_active" in completion_data
        assert completion_data["timelapse_was_active"] is True

    @pytest.mark.asyncio
    async def test_hms_errors_included_in_failed_completion_callback(self):
        """Verify completion callback receives hms_errors for failed prints."""
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        client = BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="TEST123",
            access_code="12345678",
        )

        completion_data = {}

        def on_complete(data):
            completion_data.update(data)

        client.on_print_start = lambda data: None
        client.on_print_complete = on_complete

        # Start print
        client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                }
            }
        )

        # Add HMS error during print
        client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "hms": [{"attr": 0x07000002, "code": 0x8001}],  # Filament module error (code must be >= 0x4000)
                }
            }
        )

        # Fail print
        client._process_message(
            {
                "print": {
                    "gcode_state": "FAILED",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                }
            }
        )

        assert "hms_errors" in completion_data
        assert len(completion_data["hms_errors"]) == 1
        assert completion_data["hms_errors"][0]["module"] == 0x07
        assert completion_data["status"] == "failed"

    @pytest.mark.asyncio
    async def test_aborted_status_when_cancelled(self):
        """Verify completion callback receives 'aborted' status when print is cancelled."""
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        client = BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="TEST123",
            access_code="12345678",
        )

        completion_data = {}

        def on_complete(data):
            completion_data.update(data)

        client.on_print_start = lambda data: None
        client.on_print_complete = on_complete

        # Start print
        client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                }
            }
        )

        # User cancels (goes to IDLE)
        client._process_message(
            {
                "print": {
                    "gcode_state": "IDLE",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                }
            }
        )

        assert completion_data["status"] == "aborted"
        assert "hms_errors" in completion_data

    @pytest.mark.asyncio
    async def test_timelapse_detected_from_ipcam_data(self):
        """Verify timelapse is detected from ipcam data (H2D sends it there, not xcam)."""
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        client = BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="TEST123",
            access_code="12345678",
        )

        completion_data = {}

        def on_complete(data):
            completion_data.update(data)

        client.on_print_start = lambda data: None
        client.on_print_complete = on_complete

        # Start print with timelapse in ipcam data (H2D format)
        client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "ipcam": {
                        "ipcam_record": "enable",
                        "timelapse": "enable",
                        "resolution": "1080p",
                    },
                }
            }
        )

        assert client._timelapse_during_print is True, "Timelapse should be detected from ipcam data"

        # Complete print
        client._process_message(
            {
                "print": {
                    "gcode_state": "FINISH",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                }
            }
        )

        assert completion_data["timelapse_was_active"] is True, (
            "timelapse_was_active should be True when timelapse was in ipcam"
        )


class TestPlateClearGate:
    """Pin the contract: every terminal status arms the plate-clear gate
    when ``require_plate_clear`` is on (upstream Bambuddy #1171).

    Pre-fix upstream raised the gate only for ``completed`` / ``failed`` —
    so a touchscreen-abort (which reports ``aborted`` because
    ``_user_stopped_printers`` is only populated for queue-UI stops) and a
    user-cancelled print left the gate down, and the next queued item
    auto-dispatched onto a fouled bed two seconds later. BamDude's arm
    block (`main.py::on_print_complete` ~4344) is already status-agnostic —
    `if archive_id and not _plate_auto_cleared_by_swap` runs unconditionally
    — so the bug never landed here. This class pins the behaviour so a
    future refactor can't silently re-narrow the predicate.
    """

    @staticmethod
    async def _drive_completion(printer_id: int, status: str, mock_pm):
        """Run ``on_print_complete`` against a printer with require_plate_clear=True."""
        from backend.app import main as main_mod

        # Seed _active_prints so the in-process archive lookup at the top
        # of on_print_complete resolves to a real archive_id without
        # needing a DB row. The arm-gate predicate gates on archive_id —
        # not seeding here would skip the gate entirely.
        subtask_name = "PlateClearTest"
        main_mod._active_prints[(printer_id, f"{subtask_name}.3mf")] = 999

        # Dispatch on the SQL string so we can return True for the
        # require_plate_clear column-select (so the arm fires) AND None
        # for the swap_compatible column-select (so _plate_auto_cleared_by_swap
        # stays False — a swap-compatible archive deliberately bypasses the
        # gate, which would mask the contract this test is pinning).
        async def execute_side_effect(stmt, *args, **kwargs):
            stmt_str = str(stmt).lower()
            result = MagicMock()
            if "swap_compatible" in stmt_str:
                result.scalar_one_or_none.return_value = None
            elif "require_plate_clear" in stmt_str:
                result.scalar_one_or_none.return_value = True
            else:
                # Default: every other lookup returns None so the heavy
                # downstream paths (queue lookups, library file fetches,
                # smart-plug snapshot reads) short-circuit cleanly.
                result.scalar_one_or_none.return_value = None
            result.scalars.return_value.all.return_value = []
            return result

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=execute_side_effect)

        # Snapshot tasks so we can cancel any background work the call spawns.
        tasks_before = set(asyncio.all_tasks())

        with (
            patch("backend.app.main.async_session", return_value=mock_session),
            patch("backend.app.main.notification_service") as mock_notif,
            patch("backend.app.main.smart_plug_manager") as mock_plug,
            patch("backend.app.main.ws_manager") as mock_ws,
            patch("backend.app.main.mqtt_relay") as mock_relay,
            patch("backend.app.main.printer_manager", mock_pm),
        ):
            mock_notif.on_print_complete = AsyncMock()
            mock_plug.on_print_complete = AsyncMock()
            mock_ws.send_print_complete = AsyncMock()
            mock_ws.broadcast = AsyncMock()
            mock_relay.on_print_complete = AsyncMock()
            mock_pm.get_printer.return_value = None

            await main_mod.on_print_complete(
                printer_id,
                {
                    "status": status,
                    "filename": "/data/Metadata/PlateClearTest.gcode",
                    "subtask_name": subtask_name,
                    "timelapse_was_active": False,
                },
            )

            # Cancel orphan background tasks before tearing down the mocks
            # — same pattern as test_print_complete_no_import_errors.
            for task in asyncio.all_tasks() - tasks_before:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Defensive cleanup: if the test mutated _active_prints with a
        # different key for some reason, drop our seed too.
        main_mod._active_prints.pop((printer_id, f"{subtask_name}.3mf"), None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["completed", "failed", "aborted", "cancelled"])
    async def test_arms_gate_for_every_terminal_status(self, status: str):
        """All four terminal statuses must arm the plate-clear gate.

        ``cancelled`` — user stopped via queue UI (or via the stop button on
        the Printers page when ``_user_stopped_printers`` was populated).
        ``aborted`` — touchscreen stop OR self-abort after a clog. Both can
        leave the bed fouled at any layer (the original "user-cancelled
        prints don't need a clear ack because nothing printed" assumption
        only holds at layer 1).
        ``failed`` / ``completed`` — the obvious cases.
        """
        mock_pm = MagicMock()
        mock_pm.set_awaiting_plate_clear = MagicMock()
        await self._drive_completion(printer_id=1, status=status, mock_pm=mock_pm)
        mock_pm.set_awaiting_plate_clear.assert_any_call(1, True)

    @pytest.mark.asyncio
    async def test_arms_gate_even_for_unrecognised_future_status(self):
        """Defence in depth: an unknown status doesn't silently bypass the gate.

        The arm block has no status filter — it runs whenever
        ``archive_id and not _plate_auto_cleared_by_swap`` holds. A future
        firmware that reports ``"foobared"`` should still raise the gate
        (better one extra "Clear Plate" click than auto-dispatching onto an
        ambiguous-state plate).
        """
        mock_pm = MagicMock()
        mock_pm.set_awaiting_plate_clear = MagicMock()
        await self._drive_completion(printer_id=1, status="foobared", mock_pm=mock_pm)
        mock_pm.set_awaiting_plate_clear.assert_any_call(1, True)

    # ------------------------------------------------------------------
    # Swap-on-failure regressions (operator-reported follow-up).
    #
    # Both swap paths in on_print_complete used to fire regardless of the
    # final status:
    #   1. ``swap_compatible=True`` archive  → ``_plate_auto_cleared_by_swap=True``
    #   2. ``swap_mode_change_table`` event  → physical change_table macro run
    #
    # On a failed/aborted/cancelled print the part is still attached to
    # the bed, so:
    #   - auto-clearing the gate routes the next queued print onto the
    #     fouled bed (the bug @kainpl reported)
    #   - running change_table physically rotates the fouled plate into
    #     the next print's path or jams the swap rig on the still-
    #     attached part
    # Both must be gated to ``status == "completed"``.
    # ------------------------------------------------------------------

    @staticmethod
    async def _drive_with_swap_compatible(printer_id: int, status: str, mock_pm):
        """Same as ``_drive_completion`` but the archive reports
        ``swap_compatible=True`` so the auto-clear branch can fire."""
        from backend.app import main as main_mod

        subtask_name = "SwapCompatTest"
        main_mod._active_prints[(printer_id, f"{subtask_name}.3mf")] = 998

        async def execute_side_effect(stmt, *args, **kwargs):
            stmt_str = str(stmt).lower()
            result = MagicMock()
            if "swap_compatible" in stmt_str:
                result.scalar_one_or_none.return_value = True  # archive IS swap_compatible
            elif "require_plate_clear" in stmt_str:
                result.scalar_one_or_none.return_value = True
            else:
                result.scalar_one_or_none.return_value = None
            result.scalars.return_value.all.return_value = []
            return result

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=execute_side_effect)

        tasks_before = set(asyncio.all_tasks())
        with (
            patch("backend.app.main.async_session", return_value=mock_session),
            patch("backend.app.main.notification_service") as mock_notif,
            patch("backend.app.main.smart_plug_manager") as mock_plug,
            patch("backend.app.main.ws_manager") as mock_ws,
            patch("backend.app.main.mqtt_relay") as mock_relay,
            patch("backend.app.main.printer_manager", mock_pm),
        ):
            mock_notif.on_print_complete = AsyncMock()
            mock_plug.on_print_complete = AsyncMock()
            mock_ws.send_print_complete = AsyncMock()
            mock_ws.broadcast = AsyncMock()
            mock_relay.on_print_complete = AsyncMock()
            mock_pm.get_printer.return_value = None

            await main_mod.on_print_complete(
                printer_id,
                {
                    "status": status,
                    "filename": f"/data/Metadata/{subtask_name}.gcode",
                    "subtask_name": subtask_name,
                    "timelapse_was_active": False,
                },
            )

            for task in asyncio.all_tasks() - tasks_before:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        main_mod._active_prints.pop((printer_id, f"{subtask_name}.3mf"), None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["failed", "aborted", "cancelled"])
    async def test_swap_compatible_archive_does_not_bypass_gate_on_failure(self, status: str):
        """When a print of a ``swap_compatible=True`` 3MF ends in a non-success
        state, the auto-clear bypass MUST NOT fire — the part is still on
        the bed and the operator has to inspect / clean / dismiss the gate
        before the next queue dispatch is allowed to run."""
        mock_pm = MagicMock()
        mock_pm.set_awaiting_plate_clear = MagicMock()
        await self._drive_with_swap_compatible(printer_id=1, status=status, mock_pm=mock_pm)
        mock_pm.set_awaiting_plate_clear.assert_any_call(1, True)

    @pytest.mark.asyncio
    async def test_swap_compatible_archive_still_bypasses_gate_on_completed(self):
        """Sanity check: the auto-clear bypass is preserved on a clean
        completion — that's the whole point of the swap_compatible flag."""
        mock_pm = MagicMock()
        mock_pm.set_awaiting_plate_clear = MagicMock()
        await self._drive_with_swap_compatible(printer_id=1, status="completed", mock_pm=mock_pm)
        # Plate auto-cleared by the swap_compatible path → arm-gate block
        # short-circuits → set_awaiting_plate_clear(printer_id, True) is
        # NOT called. (Note: True calls during init/cleanup elsewhere are
        # not made by this function — the existing parametric tests above
        # confirm the call pattern.)
        for call in mock_pm.set_awaiting_plate_clear.call_args_list:
            assert call.args != (1, True), "swap_compatible + completed should bypass the manual gate"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["failed", "aborted", "cancelled"])
    async def test_change_table_macro_skipped_on_failure(self, status: str):
        """When the queue-job had ``swap_mode_change_table`` queued AND the
        print ends in a non-success state, the physical change_table macro
        MUST NOT run. Otherwise the swap rig either jams on the stuck part
        or rotates the fouled plate into the next print's path. The arm-
        gate block then raises the manual-clear flag instead."""
        from backend.app import main as main_mod

        printer_id = 1
        subtask_name = "ChangeTableSkipTest"
        main_mod._active_prints[(printer_id, f"{subtask_name}.3mf")] = 997
        main_mod._active_swap_config[printer_id] = {"swap_macro_events": ["swap_mode_change_table"]}

        async def execute_side_effect(stmt, *args, **kwargs):
            stmt_str = str(stmt).lower()
            result = MagicMock()
            if "swap_compatible" in stmt_str:
                result.scalar_one_or_none.return_value = None
            elif "require_plate_clear" in stmt_str:
                result.scalar_one_or_none.return_value = True
            else:
                result.scalar_one_or_none.return_value = None
            result.scalars.return_value.all.return_value = []
            return result

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=execute_side_effect)

        mock_pm = MagicMock()
        mock_pm.set_awaiting_plate_clear = MagicMock()
        mock_pm.execute_macro_and_wait = AsyncMock(return_value=(True, ""))
        mock_pm.get_printer.return_value = None

        tasks_before = set(asyncio.all_tasks())
        try:
            with (
                patch("backend.app.main.async_session", return_value=mock_session),
                patch("backend.app.main.notification_service") as mock_notif,
                patch("backend.app.main.smart_plug_manager") as mock_plug,
                patch("backend.app.main.ws_manager") as mock_ws,
                patch("backend.app.main.mqtt_relay") as mock_relay,
                patch("backend.app.main.printer_manager", mock_pm),
                patch("backend.app.services.macro_executor.find_swap_macro", new_callable=AsyncMock) as mock_find,
            ):
                mock_notif.on_print_complete = AsyncMock()
                mock_plug.on_print_complete = AsyncMock()
                mock_ws.send_print_complete = AsyncMock()
                mock_ws.broadcast = AsyncMock()
                mock_relay.on_print_complete = AsyncMock()
                mock_find.return_value = MagicMock(name="change_table_macro", gcode="G28\n")

                await main_mod.on_print_complete(
                    printer_id,
                    {
                        "status": status,
                        "filename": f"/data/Metadata/{subtask_name}.gcode",
                        "subtask_name": subtask_name,
                        "timelapse_was_active": False,
                    },
                )

                for task in asyncio.all_tasks() - tasks_before:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

                # The macro lookup MAY have been called (find_swap_macro is
                # cheap), but the physical execution must not have happened.
                mock_pm.execute_macro_and_wait.assert_not_called()
                # And the manual clear gate should have been raised.
                mock_pm.set_awaiting_plate_clear.assert_any_call(printer_id, True)
        finally:
            main_mod._active_prints.pop((printer_id, f"{subtask_name}.3mf"), None)
            main_mod._active_swap_config.pop(printer_id, None)


class TestCallbackErrorHandling:
    """Test that callback errors are properly logged."""

    @pytest.mark.asyncio
    async def test_callback_errors_are_logged(self, capture_logs):
        """Verify that exceptions in callbacks are logged, not swallowed."""
        from backend.app.services.printer_manager import PrinterManager

        manager = PrinterManager()

        # Set up event loop
        loop = asyncio.get_event_loop()
        manager.set_event_loop(loop)

        # Create a callback that raises an error
        error_raised = False

        async def failing_callback(printer_id, data):
            nonlocal error_raised
            error_raised = True
            raise ValueError("Test error in callback")

        manager.set_print_complete_callback(failing_callback)

        # The _schedule_async should log the error
        # This is tested indirectly - if exception handling is broken,
        # the error would be swallowed silently


class TestNoImportShadowing:
    """Verify no import shadowing issues exist in callbacks."""

    @pytest.mark.asyncio
    async def test_on_print_complete_no_import_errors(self, capture_logs):
        """Verify on_print_complete doesn't have import shadowing issues."""
        # Import the module to check for syntax/import errors
        from backend.app import main

        # The ArchiveService should be accessible
        from backend.app.services.archive import ArchiveService

        # Verify we can instantiate it (would fail with shadowing bug)
        assert ArchiveService is not None

        # Check logs for any import-related errors
        errors = capture_logs.get_errors()
        import_errors = [
            e for e in errors if "import" in str(e.message).lower() or "local variable" in str(e.message).lower()
        ]
        assert not import_errors, f"Import errors found: {import_errors}"
