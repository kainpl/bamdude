"""Where the Notify! hooks sit in BamDude's own callbacks, and that a Notify! failure never stops one.

Upstream's ``test_notify_callback_isolation.py`` proves the second half against upstream's
``main.py``; ours differs (a stale-client and a deleted-record fence before the start hook, the
status snapshot taken on the MQTT thread), so the same intent is written against our callbacks
(spec A4-A6, A8, A12).
"""

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from backend.app import main
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.notify_live_activities import notify_live_activities

pytestmark = pytest.mark.asyncio


class ReachedNextStep(Exception):
    """Stop a large callback once the step after the hook was reached."""


def _start_data():
    return {
        "filename": "Benchy.gcode.3mf",
        "subtask_name": "Benchy",
        "_bamdude_client_generation": 1,
        "_bamdude_printer_life": 1,
    }


async def test_stale_client_print_start_never_reaches_notify():
    with (
        patch.object(main.printer_manager, "accepts_client_callback_generation", return_value=False),
        patch.object(notify_live_activities, "print_started") as started,
    ):
        await main.on_print_start(42, _start_data())
    started.assert_not_called()


async def test_deleted_record_print_start_never_reaches_notify():
    with (
        patch.object(main.printer_manager, "accepts_client_callback_generation", return_value=True),
        patch.object(main.printer_manager, "life_holds", return_value=False),
        patch.object(notify_live_activities, "print_started") as started,
    ):
        await main.on_print_start(42, _start_data())
    started.assert_not_called()


async def test_internal_job_still_starts_a_tile_and_a_notify_failure_stops_nothing():
    """Upstream follows calibrations too: the hook sits before the internal-job return."""
    with (
        patch.object(main.printer_manager, "accepts_client_callback_generation", return_value=True),
        patch.object(main.printer_manager, "life_holds", return_value=True),
        patch.object(main.printer_manager, "get_status", return_value=PrinterState()),
        patch.object(main, "is_internal_printer_job", return_value=True) as internal,
        patch.object(notify_live_activities, "print_started", side_effect=RuntimeError("Notify failed")) as started,
    ):
        await main.on_print_start(42, _start_data())
    started.assert_called_once()
    internal.assert_called_once()


async def test_status_snapshot_is_taken_on_the_producer_thread_and_a_notify_failure_stops_nothing(monkeypatch):
    state = PrinterState()
    state.connected = True
    state.state = "RUNNING"
    state.progress = 10
    state.subtask_id = "job1"
    seen = []

    def observe(printer_id, snapshot):
        seen.append((printer_id, snapshot.progress))
        raise RuntimeError("Notify failed")

    monkeypatch.setattr(notify_live_activities, "_provider_enabled", True)
    with (
        patch.object(notify_live_activities, "observe_snapshot", side_effect=observe),
        patch("backend.app.services.interrupted_dispatch.on_status"),
        patch.object(main, "_record_hms_faults", AsyncMock(side_effect=ReachedNextStep)),
    ):
        coroutine = main.on_printer_status_change(42, state)
        state.progress = 90  # the next packet rewrites the state before the loop runs the handler
        with pytest.raises(ReachedNextStep):
            await coroutine
    assert seen == [(42, 10)]


async def test_a_dormant_integration_takes_no_snapshot(monkeypatch):
    monkeypatch.setattr(notify_live_activities, "_provider_enabled", False)
    with (
        patch("backend.app.services.notify_live_activities.PrintSnapshot.from_state") as snapshot,
        patch("backend.app.services.interrupted_dispatch.on_status"),
        patch.object(main, "_record_hms_faults", AsyncMock(side_effect=ReachedNextStep)),
        pytest.raises(ReachedNextStep),
    ):
        await main.on_printer_status_change(42, PrinterState())
    snapshot.assert_not_called()


async def test_print_finished_is_told_and_a_notify_failure_stops_nothing():
    data = {"status": "completed", "subtask_id": "job1"}
    with (
        patch.object(notify_live_activities, "print_finished", side_effect=RuntimeError("Notify failed")) as finished,
        patch.object(main, "_release_unseeded_journal", AsyncMock(side_effect=ReachedNextStep)),
        pytest.raises(ReachedNextStep),
    ):
        await main.on_print_complete(42, data)
    finished.assert_called_once_with(42, data)


async def test_the_workers_start_and_stop_with_the_app():
    source = inspect.getsource(main._application_lifespan)
    assert "notify_live_activities.start()" in source and "notify_widgets.start()" in source
    assert "await notify_live_activities.close()" in source and "await notify_widgets.close()" in source


async def test_restore_closes_both_notify_workers():
    from backend.app.api.routes import settings as settings_routes

    source = inspect.getsource(settings_routes._restore_backup)
    assert "await notify_live_activities.close()" in source and "await notify_widgets.close()" in source


async def test_printer_delete_forgets_its_notify_print(async_client, printer_factory):
    printer = await printer_factory(name="doomed")
    with patch.object(notify_live_activities, "forget_printer") as forget:
        response = await async_client.delete(f"/api/v1/printers/{printer.id}?delete_archives=true")
    assert response.status_code == 200, response.text
    forget.assert_called_once_with(printer.id)
