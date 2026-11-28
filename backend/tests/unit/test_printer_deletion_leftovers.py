"""What a deleted printer leaves behind must not greet the next printer with its id."""

import pytest

from backend.app.services import plate_detection, printer_deletion

# Each was keyed by the printer's id and survived its deletion until upstream 578add28d was audited.
LEFT_BEHIND = [
    ("backend.app.services.obico_detection", "obico_detection_service._states"),
    ("backend.app.services.obico_detection", "obico_detection_service._state_keys"),
    ("backend.app.services.obico_detection", "obico_detection_service._last_class"),
    ("backend.app.services.obico_detection", "obico_detection_service._errors"),
    ("backend.app.services.obico_detection", "obico_detection_service._action_fired"),
    ("backend.app.services.obico_detection", "obico_detection_service._print_ids"),
    ("backend.app.services.obico_detection", "obico_detection_service._alerted"),
    ("backend.app.services.obico_detection", "obico_detection_service._notified_at"),
    ("backend.app.services.obico_detection", "obico_detection_service._baselines"),
    ("backend.app.services.print_scheduler", "scheduler._dispatch_holds"),
    ("backend.app.services.print_scheduler", "scheduler._drying_in_progress"),
    ("backend.app.services.print_scheduler", "scheduler._scheduled_drying_printers"),
    ("backend.app.services.print_scheduler", "scheduler._scheduled_dry_units"),
    ("backend.app.services.print_scheduler", "scheduler._armed_dry_cycles"),
    ("backend.app.services.print_scheduler", "scheduler._dry_unit_history"),
    ("backend.app.services.print_scheduler", "scheduler._auto_dry_above"),
    ("backend.app.services.filament_low", "_announced"),
    ("backend.app.services.connection_watchdog", "_last_rebuild"),
    ("backend.app.services.auto_queue_scheduler", "auto_queue_scheduler._wake_cooldowns"),
    ("backend.app.services.auto_queue_scheduler", "auto_queue_scheduler._confirmed_wakes"),
    ("backend.app.api.routes.printers", "_cover_cache"),
    ("backend.app.api.routes.printers", "_plates_cache"),
    ("backend.app.services.chamber_history", "_history"),
    ("backend.app.services.pause_warning", "_runs"),
    ("backend.app.services.mqtt_relay", "mqtt_relay._last_printer_status"),
    ("backend.app.services.mqtt_relay", "mqtt_relay._last_printer_connected"),
    ("backend.app.services.mqtt_relay", "mqtt_relay._last_status_payload"),
    ("backend.app.services.timelapse_files", "_listing_answered"),
    ("backend.app.services.macro_trigger", "_fired_layer_macros"),
    ("backend.app.services.interrupted_dispatch", "_waiting"),
    ("backend.app.services.ams_backup_compatibility_apply", "_deferred_attempts"),
    ("backend.app.services.ams_sync_debounce", "debounce._candidates"),
    ("backend.app.main", "_active_print_identities"),
    ("backend.app.main", "_last_projection_broadcast"),
    ("backend.app.main", "_projection_repeat_available"),
    ("backend.app.services.wled", "wled_manager._configs"),
    ("backend.app.services.wled", "wled_manager._require_plate_clear"),
    ("backend.app.services.wled", "wled_manager._runtime"),
]


@pytest.mark.parametrize("entry", LEFT_BEHIND, ids=lambda e: e[1])
def test_the_registry_leaves_with_the_printer(entry):
    assert entry in printer_deletion._ID_KEYED
    container = printer_deletion._container(*entry)
    assert isinstance(container, (dict, set))


def test_an_archives_ams_mapping_is_not_taken_for_a_printer_with_the_same_number():
    # The store is keyed by ARCHIVE id: taking "printer 7" out of it dropped archive 7's mapping.
    names = {name for _module, name in printer_deletion._ID_KEYED}
    assert "_print_ams_mappings" not in names


def test_keys_of_another_printer_stay():
    container = {5: "a", (5, 1): "b", 50: "c", (50, 1): "d"}

    taken = printer_deletion._take(container, 5)

    assert taken == {5: "a", (5, 1): "b"}
    assert container == {50: "c", (50, 1): "d"}


@pytest.fixture
def calibration_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(plate_detection, "_get_calibration_dir", lambda: tmp_path)
    for name in (
        "printer_5_ref_0.jpg",
        "printer_5_ref_3.jpg",
        "printer_5_metadata.json",
        "printer_50_ref_0.jpg",
        "printer_50_metadata.json",
    ):
        (tmp_path / name).write_bytes(b"x")
    return tmp_path


def test_plate_references_of_a_deleted_printer_are_removed(calibration_dir):
    removed = plate_detection.forget_printer_references(5)

    assert removed == 3
    assert sorted(p.name for p in calibration_dir.iterdir()) == ["printer_50_metadata.json", "printer_50_ref_0.jpg"]


def test_a_confirmed_deletion_removes_the_references(calibration_dir, monkeypatch):
    from backend.app.services import print_run_binding
    from backend.app.services.printer_manager import printer_manager

    monkeypatch.setattr(printer_manager, "end_printer_deletion", lambda *a, **k: None)
    monkeypatch.setattr(print_run_binding, "retire_printer_runtime", lambda *a, **k: None)
    deletion = printer_deletion.PrinterDeletion(printer_id=5, life=1, effect_boundary=0, resolution_boundary=0)

    printer_deletion.finish(deletion, committed=False)
    assert (calibration_dir / "printer_5_ref_0.jpg").exists()

    printer_deletion.finish(deletion, committed=True)
    assert not (calibration_dir / "printer_5_ref_0.jpg").exists()
    assert (calibration_dir / "printer_50_ref_0.jpg").exists()


@pytest.mark.asyncio
async def test_a_committed_deletion_cancels_the_wled_tasks():
    """N02 A3: the dropped runtime's send and Finished timer (up to a day) are cancelled, not left to wake."""
    import asyncio

    from backend.app.services.wled import _WLEDRuntime, wled_manager

    send, timer = asyncio.create_task(asyncio.sleep(3600)), asyncio.create_task(asyncio.sleep(3600))
    wled_manager._runtime[5] = _WLEDRuntime(send_task=send, finished_task=timer)
    try:
        taken = printer_deletion._take(wled_manager._runtime, 5)
        assert 5 not in wled_manager._runtime
        printer_deletion._drop(taken)
        await asyncio.gather(send, timer, return_exceptions=True)
        assert send.cancelled() and timer.cancelled()
    finally:
        wled_manager._runtime.pop(5, None)


@pytest.mark.asyncio
async def test_a_rolled_back_deletion_keeps_the_wled_runtime():
    import asyncio

    from backend.app.services.wled import _WLEDRuntime, wled_manager

    timer = asyncio.create_task(asyncio.sleep(3600))
    runtime = _WLEDRuntime(finished_task=timer)
    wled_manager._runtime[5] = runtime
    try:
        taken = printer_deletion._take(wled_manager._runtime, 5)
        printer_deletion._put_back(wled_manager._runtime, taken)
        assert wled_manager._runtime[5] is runtime
        assert not timer.done()
    finally:
        timer.cancel()
        wled_manager._runtime.pop(5, None)
