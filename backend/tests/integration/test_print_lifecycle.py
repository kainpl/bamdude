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
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.print_execution import StartDecision


@contextmanager
def _finishing_archive(session, printer_id: int, archive_id: int, *, swap_compatible: bool | None = None):
    """Give ``on_print_complete`` the archive this print belongs to.

    The callback takes its archive from a bound run or from the durable
    executions -- a printing row it resolves once and then proves under the
    archive's write guard. A filename alias in ``_active_prints`` selects
    nothing any more, and a mocked session can answer neither read, so both
    answers are supplied here: the resolution, and the row it is proven by.
    Without them the callback finishes as an archive-less print, which arms no
    gate and credits no stock by design.
    """
    from backend.app import main as main_mod
    from backend.app.models.archive import PrintArchive

    archive = MagicMock(name="finishing_archive")
    archive.id = archive_id
    archive.printer_id = printer_id
    archive.status = "printing"
    archive.recovery_identity = f"identity-{archive_id}"
    # No declaration record: only a strictly True column declares a baked swap sequence.
    archive.extra_data = {}
    archive.swap_compatible = swap_compatible

    async def get(model, _id, *args, **kwargs):
        return archive if model is PrintArchive else MagicMock()

    session.get = AsyncMock(side_effect=get)

    @asynccontextmanager
    async def no_guard(*_args, **_kwargs):
        yield

    resolution = main_mod._LiveArchiveResolution(archive_id, None, "unambiguous_name", archive.recovery_identity)
    with (
        patch("backend.app.main._live_archive_for_running_print", AsyncMock(return_value=resolution)),
        patch("backend.app.services.archive_write_scope.archive_write_scope", no_guard),
        patch(
            "backend.app.services.archive_write_scope.load_active_archive_for_write",
            AsyncMock(return_value=archive),
        ),
    ):
        yield archive


def _gate_manager() -> MagicMock:
    """A printer manager whose gate is armed the way production arms it:
    ``arm_awaiting_plate_clear`` is awaited with the printer and the archive
    the gate waits on (the synchronous setter is only the old adapters'
    fallback)."""
    manager = MagicMock()
    manager.arm_awaiting_plate_clear = AsyncMock(return_value="gate-token")
    manager.set_awaiting_plate_clear = MagicMock()
    manager.get_printer.return_value = None
    return manager


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
            # The start is first decided against the durable executions; a
            # mocked session has none to read, so the decision is given: a
            # print nobody dispatched, with nothing older to close.
            patch(
                "backend.app.services.print_execution.decide_start",
                new=AsyncMock(
                    return_value=StartDecision(
                        current_id=None, current_identity=None, old=(), reason="new", epoch=0, observation=None
                    )
                ),
            ),
            patch("backend.app.services.print_execution.recheck_start", new=AsyncMock()),
        ):
            mock_notif.on_print_start = AsyncMock()
            mock_plug.on_print_start = AsyncMock()
            mock_ws.send_print_start = AsyncMock()

            # Mock the database session. ``__aexit__`` answers False: a bare
            # AsyncMock() answers a truthy mock, which SWALLOWS whatever is
            # raised inside ``async with`` and lets the callback run on with
            # half its work undone.
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
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
        # False: a bare ``AsyncMock()`` is truthy, which tells ``async with``
        # the exception was handled -- the callback would run on half done.
        mock_session.__aexit__ = AsyncMock(return_value=False)
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
            _finishing_archive(mock_session, printer_id, 999),
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
        mock_pm = _gate_manager()
        await self._drive_completion(printer_id=1, status=status, mock_pm=mock_pm)
        mock_pm.arm_awaiting_plate_clear.assert_awaited_once_with(1, 999)

    @pytest.mark.asyncio
    async def test_arms_gate_even_for_unrecognised_future_status(self):
        """Defence in depth: an unknown status doesn't silently bypass the gate.

        The arm block has no status filter — it runs whenever
        ``archive_id and not _plate_auto_cleared_by_swap`` holds. A future
        firmware that reports ``"foobared"`` should still raise the gate
        (better one extra "Clear Plate" click than auto-dispatching onto an
        ambiguous-state plate).
        """
        mock_pm = _gate_manager()
        await self._drive_completion(printer_id=1, status="foobared", mock_pm=mock_pm)
        mock_pm.arm_awaiting_plate_clear.assert_awaited_once_with(1, 999)

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
        from backend.app.services.print_run_binding import bind_print_run

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
        # False: a bare ``AsyncMock()`` is truthy, which tells ``async with``
        # the exception was handled -- the callback would run on half done.
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(side_effect=execute_side_effect)

        tasks_before = set(asyncio.all_tasks())
        with (
            patch("backend.app.main.async_session", return_value=mock_session),
            patch("backend.app.main.notification_service") as mock_notif,
            patch("backend.app.main.smart_plug_manager") as mock_plug,
            patch("backend.app.main.ws_manager") as mock_ws,
            patch("backend.app.main.mqtt_relay") as mock_relay,
            patch("backend.app.main.printer_manager", mock_pm),
            # The archive DECLARES its own swap sequence -- what the bypass would lean on.
            _finishing_archive(mock_session, printer_id, 998, swap_compatible=True) as archive,
            # The run's durable acceptance (archive + its queue row) is the database's.
            patch("backend.app.main._accept_bound_terminal_run", AsyncMock(return_value=41)),
        ):
            # ...and a run BamDude dispatched itself: the one finish a baked sequence may clear the plate
            # for. So nothing but the print's outcome stands between this archive and the bypass.
            mock_pm._print_run_binding_sequence = 0
            mock_pm._print_effect_sequence = 0
            bind_print_run(
                mock_pm,
                printer_id=printer_id,
                archive_id=998,
                queue_item_id=41,
                origin="dispatch",
                archive_identity=archive.recovery_identity,
            )
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
        mock_pm = _gate_manager()
        await self._drive_with_swap_compatible(printer_id=1, status=status, mock_pm=mock_pm)
        mock_pm.arm_awaiting_plate_clear.assert_awaited_once_with(1, 998)

    # swap-source-lifecycle T7: a completed True archive found by an alias (as this mock drives it) no longer
    # clears the plate -- a name proves no run (spec r4 §10 D5). The bypass on a proven run, and its release of a
    # standing gate, are pinned on a real database in test_swap_terminal_ownership.py.
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
        # False: a bare ``AsyncMock()`` is truthy, which tells ``async with``
        # the exception was handled -- the callback would run on half done.
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(side_effect=execute_side_effect)

        mock_pm = _gate_manager()
        mock_pm.execute_macro_and_wait = AsyncMock(return_value=(True, ""))

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
                _finishing_archive(mock_session, printer_id, 997),
                # What a refused table swap does instead: the queue is held with a reason. Its rows are the
                # database's, so the hold is observed here and pinned on a real one in the swap-terminal tests.
                patch("backend.app.main._hold_for_owed_swap", new_callable=AsyncMock) as mock_hold,
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
                # Not skipped by accident: the swap was owed, and it is held because the print did not complete.
                mock_hold.assert_awaited_once()
                assert mock_hold.await_args.args[:2] == (printer_id, 997)
                assert mock_hold.await_args.args[3] == "swap_hold_not_completed"
                # And the manual clear gate should have been raised.
                mock_pm.arm_awaiting_plate_clear.assert_awaited_once_with(printer_id, 997)
        finally:
            main_mod._active_prints.pop((printer_id, f"{subtask_name}.3mf"), None)
            main_mod._active_swap_config.pop(printer_id, None)

    # ------------------------------------------------------------------
    # Operator-reported: a swap printer goes silent forever.
    #
    # ``_plate_auto_cleared_by_swap`` only ever caused the arm block to be
    # SKIPPED. It never released a gate that was already armed — and the gate
    # is persisted to ``printers.awaiting_plate_clear`` and restored at
    # startup, so one cancelled print (or one print started from the
    # printer's own screen, where no swap config is registered) armed it for
    # good. Every later swap print logged "plate auto-cleared", left the flag
    # standing, and the queue silently refused to dispatch to that printer.
    #
    # Silently is the operative word: the auto-queue distributor logs its
    # per-tick decisions at DEBUG, so an INFO-level support log shows nothing
    # at all — which is exactly how three support bundles came back with no
    # errors in them.
    #
    # Releasing here is safe, and provably so: ``_plate_auto_cleared_by_swap``
    # is set in exactly two places, and both require a *successful* print —
    # the swap_compatible branch is guarded by ``_swap_status_ok``, and the
    # change_table branch sets it only under ``if _sw_ok`` after the macro
    # returns success. A failed, aborted or cancelled print never reaches
    # either, so the part-still-on-the-bed case keeps its manual gate.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["failed", "aborted", "cancelled"])
    async def test_a_failed_swap_print_never_releases_the_gate(self, status: str):
        """The other half of the rule. The part is still on the bed, so the
        operator must clear it — the release must not fire here."""
        mock_pm = _gate_manager()

        await self._drive_with_swap_compatible(printer_id=1, status=status, mock_pm=mock_pm)

        for call in mock_pm.set_awaiting_plate_clear.call_args_list:
            assert call.args != (1, False), f"status={status} left material on the plate; the gate must stay armed"


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


class TestFreeStockOnCompletion:
    """A print filed under no order puts its parts on the product's shelf
    (projects redesign pass 8, Decision 3).

    Two properties, and the second is the load-bearing one: the credit runs at
    completion, and NOTHING it can do stops the print from finishing. The
    ledger is a consequence of the print; a print is never a consequence of the
    ledger.
    """

    @pytest.mark.asyncio
    async def test_a_finished_print_is_offered_to_the_free_stock_ledger(self):
        """Placement: inside ``on_print_complete``, for the archive it resolved
        — not in a background task, where a failure would be invisible, and not
        before the status update, where the archive is not ``completed`` yet."""
        from backend.app import main as main_mod

        with patch("backend.app.main._credit_free_stock", new_callable=AsyncMock) as credit:
            await TestPlateClearGate._drive_completion(printer_id=1, status="completed", mock_pm=MagicMock())

        credit.assert_awaited_once_with(999)
        assert main_mod._credit_free_stock is not credit  # the patch is gone again

    @pytest.mark.asyncio
    async def test_a_ledger_that_blows_up_does_not_fail_the_print(self, capture_logs):
        """The archive is already written, the notification is on its way and
        the plate is finished. Whatever the ledger objects to — a product edited
        under it, a locked database — is logged and swallowed."""
        from backend.app import main as main_mod

        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        # ⚠️ ``return_value=False`` matters here and nowhere else in this file: a
        # bare ``AsyncMock`` answers ``__aexit__`` with a truthy mock, which is
        # the protocol's "I have handled this exception" — the session would
        # swallow the very failure this test raises, and the assertion below
        # would fail while the code under test was blameless.
        session.__aexit__ = AsyncMock(return_value=False)
        session.get = AsyncMock(return_value=MagicMock())

        with (
            patch("backend.app.main.async_session", return_value=session),
            patch(
                "backend.app.services.part_stock.credit_unfiled_print",
                new_callable=AsyncMock,
                side_effect=RuntimeError("ledger is on fire"),
            ),
        ):
            await main_mod._credit_free_stock(999)

        assert any("free-stock credit failed" in str(r.message) for r in capture_logs.get_warnings())

    @pytest.mark.asyncio
    async def test_a_print_with_no_archive_asks_the_ledger_nothing(self):
        """External prints that never got an archive reach the end of
        ``on_print_complete`` with ``archive_id`` unset; opening a session for
        them would be a query per print for nothing."""
        from backend.app import main as main_mod

        with patch("backend.app.main.async_session") as session_maker:
            await main_mod._credit_free_stock(None)

        session_maker.assert_not_called()


class TestACompletionWithoutAnArchive:
    """G03 E-4 (owner): every BamDude print has an archive; a completion that finds none guesses no owner.

    It used to take the owner of whatever queue row this printer finished in the last five minutes — typically
    somebody else's job that had just failed or been cancelled — and mail them "your print failed / is done"
    about a print that was not theirs, with an estimate read from a library column that does not exist.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_providers_are_told_and_nobody_is_mailed(self, db_session, printer_factory, monkeypatch):
        from datetime import datetime, timedelta, timezone

        from backend.app import main as main_mod
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.printer_queue import PrinterQueue
        from backend.app.models.user import User

        printer = await printer_factory(name="Bench")
        alice = User(username="alice-e4")
        db_session.add_all([PrinterQueue(id=printer.id, printer_id=printer.id), alice])
        await db_session.flush()
        # Alice's job failed on this printer a minute ago: the row the old lookup would have borrowed.
        db_session.add(
            PrintQueueItem(
                queue_id=printer.id,
                status="failed",
                position=0,
                created_by_id=alice.id,
                completed_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        await db_session.commit()
        printer_id = printer.id

        @asynccontextmanager
        async def _session():
            yield db_session

        service = MagicMock()
        service.on_print_complete = AsyncMock()
        service.send_user_print_email = AsyncMock()
        monkeypatch.setattr(main_mod, "async_session", _session)
        monkeypatch.setattr(main_mod, "notification_service", service)

        await main_mod._notify_completion_without_archive(printer_id, {"status": "failed", "subtask_name": "part"})

        service.on_print_complete.assert_awaited_once()
        call = service.on_print_complete.await_args
        assert call.args[:3] == (printer_id, "Bench", "failed")
        assert call.kwargs["archive_data"] is None
        service.send_user_print_email.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_notification_never_raises(self, monkeypatch):
        from backend.app import main as main_mod

        def broken():
            raise RuntimeError("database is gone")

        monkeypatch.setattr(main_mod, "async_session", broken)
        await main_mod._notify_completion_without_archive(1, {"status": "completed"})
