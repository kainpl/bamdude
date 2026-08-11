"""Tests for the connected → disconnected edge that fires the
`on_printer_offline` notification (#1752).

The provider toggle, schema, and dispatcher already existed; what was missing
was a caller that fires `notification_service.on_printer_offline` when a
printer goes offline. These tests pin both layers:

  * `_maybe_notify_printer_offline` — the debounced background task. Must
    fire when the printer is still offline at the end of the window, and
    must NOT fire if the printer reconnected during the window.

  * Edge detection inside `on_printer_status_change` — schedules the task
    (via ``spawn_background_task``) only on the True → False transition,
    cancels any pending task on reconnect, and stays silent on startup (no
    prior connected state).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app import main as main_module


def _state(connected: bool, state: str = "IDLE") -> SimpleNamespace:
    """Minimal PrinterState stub carrying just the fields
    ``on_printer_status_change`` reads before the offline edge block.

    ⚠️ This list is a dependency, not decoration: the function builds its
    broadcast key from these, so a field added there has to be added here or
    every test in this file fails on an AttributeError that says nothing about
    printers going offline.
    """
    return SimpleNamespace(
        connected=connected,
        state=state,
        progress=0,
        layer_num=0,
        temperatures={},
        raw_data={},
        stg_cur=0,
        cooling_fan_speed=0,
        big_fan1_speed=0,
        big_fan2_speed=0,
        heatbreak_fan_speed=0,
        # The air duct joined the key so a fan speed or a mode change can
        # trigger a broadcast at all — on machines with one, the fans are
        # reported nowhere else.
        airduct_parts={},
        airduct_mode=0,
        airduct_sub_mode=-1,
        speed_level=2,
        sdcard=False,
        sdcard_state="",
        store_to_sdcard=False,
        timelapse=False,
        ipcam=False,
        firmware_version="",
        mc_print_sub_stage=0,
        firmware_consistency_request=False,
        firmware_force_upgrade=False,
        # The temperature bounds the dedup key reads. They belong here rather
        # than behind a getattr in ``main``: the key is meant to fail loudly when
        # a field it needs is missing, and a default would hide a real state
        # object that stopped carrying one.
        nozzle_temp_range=None,
        bed_temp_range=None,
        bed_temperature_limit=None,
        is_220v=False,
        ext_has_nozzle={},
        axis_at_home={"x": True, "y": True, "z": True},
        ext_has_filament={},
        chamber_light="",
        active_extruder=0,
        tray_now=0,
        ams_auto_switch_filament=False,
        door_open=False,
        subtask_name="",
    )


async def _blocking(*_args, **_kwargs):
    """Stand-in for the scheduled task body that never completes on its own,
    so ``task.done()`` stays False between edge calls. Cancelled by the
    autouse leaked-task cleanup (or the handler's reconnect branch)."""
    await asyncio.Event().wait()


@pytest.fixture(autouse=True)
def _reset_edge_state():
    """Clear the module-level edge dicts between tests so one test's
    True-edge doesn't leak into the next."""
    main_module._printer_last_connected.clear()
    for task in list(main_module._printer_offline_notify_tasks.values()):
        if not task.done():
            task.cancel()
    main_module._printer_offline_notify_tasks.clear()
    main_module._last_status_broadcast.clear()
    main_module._last_printer_state.clear()
    yield
    main_module._printer_last_connected.clear()
    for task in list(main_module._printer_offline_notify_tasks.values()):
        if not task.done():
            task.cancel()
    main_module._printer_offline_notify_tasks.clear()
    main_module._last_status_broadcast.clear()
    main_module._last_printer_state.clear()


class TestMaybeNotifyPrinterOffline:
    """The debounced background task — fires notification at the end of the
    window only if the printer is still offline."""

    async def test_fires_notification_when_still_offline_after_debounce(self):
        printer = SimpleNamespace(id=1, name="Workshop")
        scalar = MagicMock()
        scalar.scalar_one_or_none.return_value = printer
        db = AsyncMock()
        db.execute = AsyncMock(return_value=scalar)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=db)
        session_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.main.asyncio.sleep", new=AsyncMock()),
            patch("backend.app.main.printer_manager") as mock_pm,
            patch("backend.app.main.async_session", return_value=session_cm),
            patch("backend.app.main.notification_service") as mock_notif,
        ):
            mock_pm.is_connected.return_value = False
            mock_notif.on_printer_offline = AsyncMock()

            await main_module._maybe_notify_printer_offline(printer_id=1)

            mock_notif.on_printer_offline.assert_awaited_once_with(1, "Workshop", db)

    async def test_does_not_fire_when_printer_reconnected_during_debounce(self):
        with (
            patch("backend.app.main.asyncio.sleep", new=AsyncMock()),
            patch("backend.app.main.printer_manager") as mock_pm,
            patch("backend.app.main.notification_service") as mock_notif,
        ):
            mock_pm.is_connected.return_value = True
            mock_notif.on_printer_offline = AsyncMock()

            await main_module._maybe_notify_printer_offline(printer_id=1)

            mock_notif.on_printer_offline.assert_not_awaited()

    async def test_does_not_fire_when_printer_missing_from_db(self):
        scalar = MagicMock()
        scalar.scalar_one_or_none.return_value = None
        db = AsyncMock()
        db.execute = AsyncMock(return_value=scalar)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=db)
        session_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.main.asyncio.sleep", new=AsyncMock()),
            patch("backend.app.main.printer_manager") as mock_pm,
            patch("backend.app.main.async_session", return_value=session_cm),
            patch("backend.app.main.notification_service") as mock_notif,
        ):
            mock_pm.is_connected.return_value = False
            mock_notif.on_printer_offline = AsyncMock()

            await main_module._maybe_notify_printer_offline(printer_id=1)

            mock_notif.on_printer_offline.assert_not_awaited()

    async def test_clears_task_entry_after_run(self):
        with (
            patch("backend.app.main.asyncio.sleep", new=AsyncMock()),
            patch("backend.app.main.printer_manager") as mock_pm,
        ):
            mock_pm.is_connected.return_value = True  # No notification path
            main_module._printer_offline_notify_tasks[1] = MagicMock()
            await main_module._maybe_notify_printer_offline(printer_id=1)
            assert 1 not in main_module._printer_offline_notify_tasks


class TestOfflineEdgeDetection:
    """Edge detection inside `on_printer_status_change` — only the
    True → False transition schedules a task via ``spawn_background_task``.
    Reconnects cancel pending tasks. Startup-with-disconnected does not fire."""

    @staticmethod
    def _patch_handler_deps():
        """Patch out the heavy side-effects of `on_printer_status_change`
        (MQTT relay, WebSocket broadcast, state serializer) so we can focus
        on edge state."""
        ws_mgr = MagicMock()
        ws_mgr.send_printer_status = AsyncMock()
        relay = MagicMock()
        relay.on_printer_status = AsyncMock()
        pm = MagicMock()
        pm.get_printer.return_value = None  # Skip the relay payload branch.
        pm.get_model.return_value = ""
        return ws_mgr, relay, pm

    async def test_first_call_connected_does_not_schedule(self):
        ws_mgr, relay, pm = self._patch_handler_deps()
        with (
            patch("backend.app.main.ws_manager", ws_mgr),
            patch("backend.app.main.mqtt_relay", relay),
            patch("backend.app.main.printer_manager", pm),
            patch("backend.app.main.printer_state_to_dict", return_value={}),
        ):
            await main_module.on_printer_status_change(1, _state(connected=True))
        assert 1 not in main_module._printer_offline_notify_tasks
        assert main_module._printer_last_connected[1] is True

    async def test_first_call_disconnected_does_not_schedule(self):
        """Startup with an already-offline printer must not fire — there's
        no prior True observation, so we have no edge to trigger on."""
        ws_mgr, relay, pm = self._patch_handler_deps()
        with (
            patch("backend.app.main.ws_manager", ws_mgr),
            patch("backend.app.main.mqtt_relay", relay),
            patch("backend.app.main.printer_manager", pm),
            patch("backend.app.main.printer_state_to_dict", return_value={}),
        ):
            await main_module.on_printer_status_change(1, _state(connected=False))
        assert 1 not in main_module._printer_offline_notify_tasks
        assert main_module._printer_last_connected[1] is False

    async def test_connected_to_disconnected_schedules_task(self):
        ws_mgr, relay, pm = self._patch_handler_deps()
        with (
            patch("backend.app.main.ws_manager", ws_mgr),
            patch("backend.app.main.mqtt_relay", relay),
            patch("backend.app.main.printer_manager", pm),
            patch("backend.app.main._maybe_notify_printer_offline", new=_blocking),
            patch("backend.app.main.printer_state_to_dict", return_value={}),
        ):
            await main_module.on_printer_status_change(1, _state(connected=True))
            await main_module.on_printer_status_change(1, _state(connected=False))

            task = main_module._printer_offline_notify_tasks.get(1)
            assert task is not None
            assert isinstance(task, asyncio.Task)
            task.cancel()

    async def test_reconnect_cancels_pending_task(self):
        ws_mgr, relay, pm = self._patch_handler_deps()
        with (
            patch("backend.app.main.ws_manager", ws_mgr),
            patch("backend.app.main.mqtt_relay", relay),
            patch("backend.app.main.printer_manager", pm),
            patch("backend.app.main._maybe_notify_printer_offline", new=_blocking),
            patch("backend.app.main.printer_state_to_dict", return_value={}),
        ):
            await main_module.on_printer_status_change(1, _state(connected=True))
            await main_module.on_printer_status_change(1, _state(connected=False))
            scheduled = main_module._printer_offline_notify_tasks.get(1)
            assert scheduled is not None
            await main_module.on_printer_status_change(1, _state(connected=True))
            # Yield so the cancellation propagates through the event loop.
            await asyncio.sleep(0)

        assert 1 not in main_module._printer_offline_notify_tasks
        assert scheduled.cancelled() or scheduled.done()

    async def test_repeated_disconnected_does_not_reschedule(self):
        """A second False observation while a task is already pending must
        not replace the in-flight task — otherwise the debounce clock
        resets on every status callback and the notification never fires."""
        ws_mgr, relay, pm = self._patch_handler_deps()
        with (
            patch("backend.app.main.ws_manager", ws_mgr),
            patch("backend.app.main.mqtt_relay", relay),
            patch("backend.app.main.printer_manager", pm),
            patch("backend.app.main._maybe_notify_printer_offline", new=_blocking),
            patch("backend.app.main.printer_state_to_dict", return_value={}),
        ):
            await main_module.on_printer_status_change(1, _state(connected=True))
            await main_module.on_printer_status_change(1, _state(connected=False))
            first_task = main_module._printer_offline_notify_tasks.get(1)
            await main_module.on_printer_status_change(1, _state(connected=False))
            second_task = main_module._printer_offline_notify_tasks.get(1)

            assert first_task is second_task
            if first_task is not None:
                first_task.cancel()
