"""Deterministic interleavings at camera, plug and macro transport boundaries."""

import asyncio
import hashlib
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.services.print_run_binding import (
    begin_print_run_finishing,
    bind_print_run,
    discard_print_run,
    effect_token_for_run,
    print_effect_is_current,
)
from backend.app.services.printer_manager import PrinterManager
from backend.app.services.smart_plug_manager import SmartPlugManager


def test_token_survives_resource_cleanup_but_never_a_successor():
    manager = PrinterManager()
    run = bind_print_run(manager, printer_id=1, archive_id=80)
    token = effect_token_for_run(run)
    begin_print_run_finishing(manager, 1, 80)
    discard_print_run(manager, 1, 80)
    assert print_effect_is_current(manager, token)
    bind_print_run(manager, printer_id=1, archive_id=81)
    begin_print_run_finishing(manager, 1, 81)
    discard_print_run(manager, 1, 81)
    assert not print_effect_is_current(manager, token)


async def test_stale_client_start_cannot_revoke_current_run(monkeypatch):
    from backend.app import main

    manager = PrinterManager()
    run = bind_print_run(manager, printer_id=1, archive_id=81)
    token = effect_token_for_run(run)
    manager._client_generations[1] = 3
    monkeypatch.setattr(main, "printer_manager", manager)
    inner = AsyncMock()
    monkeypatch.setattr(main, "_on_print_start_impl", inner)
    await main.on_print_start(1, {"subtask_id": "80", "_bamdude_client_generation": 2})
    inner.assert_not_awaited()
    assert print_effect_is_current(manager, token)


@pytest.mark.parametrize("per_job", [False, True])
async def test_plug_lookup_cannot_reschedule_old_completion(monkeypatch, per_job):
    plugs = SmartPlugManager()
    manager = PrinterManager()
    token = effect_token_for_run(bind_print_run(manager, printer_id=1, archive_id=80))
    reached = asyncio.Event()
    release = asyncio.Event()

    async def lookup(*_):
        reached.set()
        await release.wait()
        return [SimpleNamespace(enabled=True)]

    monkeypatch.setattr(plugs, "_get_plugs_for_printer", lookup)
    schedule = MagicMock()
    monkeypatch.setattr(plugs, "_schedule_off_per_mode", schedule)

    def guard():
        return print_effect_is_current(manager, token)

    callback = (
        plugs.schedule_off_after_queue_job(1, AsyncMock(), may_run=guard)
        if per_job
        else plugs.on_print_complete(1, "completed", AsyncMock(), may_run=guard)
    )
    task = asyncio.create_task(callback)
    await asyncio.wait_for(reached.wait(), timeout=5)
    bind_print_run(manager, printer_id=1, archive_id=81)
    release.set()
    await task
    schedule.assert_not_called()


async def test_swap_ack_cannot_register_waiter_after_successor(monkeypatch):
    manager = PrinterManager()
    token = effect_token_for_run(bind_print_run(manager, printer_id=1, archive_id=80))
    manager._clients[1] = MagicMock()
    reached = asyncio.Event()
    release = asyncio.Event()

    async def ack(*_):
        reached.set()
        await release.wait()
        return True, ""

    monkeypatch.setattr("backend.app.services.macro_executor.send_macro_and_await_ack", ack)
    task = asyncio.create_task(
        manager.execute_macro_and_wait(1, "M400", "swap", may_run=lambda: print_effect_is_current(manager, token))
    )
    await asyncio.wait_for(reached.wait(), timeout=5)
    bind_print_run(manager, printer_id=1, archive_id=81)
    new_waiter = (asyncio.Event(), {"status": "pending"})
    manager._macro_waiters[1] = new_waiter
    release.set()
    assert await task == (False, "Print superseded")
    assert manager._macro_waiters[1] is new_waiter


async def test_swap_wait_cleanup_cannot_remove_successor_waiter(monkeypatch):
    manager = PrinterManager()
    token = effect_token_for_run(bind_print_run(manager, printer_id=1, archive_id=80))
    manager._clients[1] = MagicMock()
    monkeypatch.setattr(
        "backend.app.services.macro_executor.send_macro_and_await_ack", AsyncMock(return_value=(True, ""))
    )
    reached = asyncio.Event()
    release = asyncio.Event()

    async def wait(_):
        reached.set()
        await release.wait()

    monkeypatch.setattr("backend.app.services.printer_manager.asyncio.sleep", wait)
    task = asyncio.create_task(
        manager.execute_macro_and_wait(1, "M400", "swap", may_run=lambda: print_effect_is_current(manager, token))
    )
    await asyncio.wait_for(reached.wait(), timeout=5)
    bind_print_run(manager, printer_id=1, archive_id=81)
    new_waiter = (asyncio.Event(), {"status": "pending"})
    manager._macro_waiters[1] = new_waiter
    release.set()
    assert await task == (False, "Print superseded")
    assert manager._macro_waiters[1] is new_waiter


@pytest.mark.parametrize("mode", ["time", "temperature"])
async def test_plug_service_lookup_rechecks_before_turn_off(mode, monkeypatch):
    plugs = SmartPlugManager()
    manager = PrinterManager()
    token = effect_token_for_run(bind_print_run(manager, printer_id=1, archive_id=80))
    reached = asyncio.Event()
    release = asyncio.Event()
    service = SimpleNamespace(turn_off=AsyncMock(return_value=True))

    async def lookup(*_):
        reached.set()
        await release.wait()
        return service

    monkeypatch.setattr(plugs, "get_service_for_plug", lookup)
    monkeypatch.setattr("backend.app.services.smart_plug_manager.printer_manager", manager)
    monkeypatch.setattr(
        manager, "get_status", lambda _: SimpleNamespace(state="IDLE", connected=True, temperatures={"nozzle": 20})
    )
    func = plugs._delayed_off if mode == "time" else plugs._temp_based_off
    task = asyncio.create_task(
        func(
            1,
            "tasmota",
            None,
            None,
            None,
            None,
            1,
            0 if mode == "time" else 50,
            may_run=lambda: print_effect_is_current(manager, token),
        )
    )
    plugs._pending_off[1] = task
    await asyncio.wait_for(reached.wait(), timeout=5)
    bind_print_run(manager, printer_id=1, archive_id=81)
    # A successor's pending task must survive the old task's finally.
    newer = asyncio.create_task(release.wait())
    plugs._pending_off[1] = newer
    release.set()
    await task
    await newer
    service.turn_off.assert_not_awaited()
    assert plugs._pending_off[1] is newer


async def test_late_camera_producer_does_not_publish_into_new_run(monkeypatch):
    from backend.app import main
    from backend.app.services.camera_metrics import CameraCaptureResult

    manager = PrinterManager()
    bind_print_run(manager, printer_id=1, archive_id=80, observed_subtask_id="80")
    monkeypatch.setattr(main, "printer_manager", manager)
    monkeypatch.setattr(main, "_completion_conflicts_with_active_queue", AsyncMock(return_value=False))
    printer = SimpleNamespace(
        external_camera_enabled=True,
        external_camera_url="http://camera",
        external_camera_type="snapshot",
        external_camera_snapshot_url=None,
    )

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: printer)))

    monkeypatch.setattr(main, "async_session", session)
    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true"))
    monkeypatch.setattr("backend.app.api.routes.camera.live_frame_for_capture", lambda _: (False, None))
    reached = asyncio.Event()
    release = asyncio.Event()

    async def capture(_):
        reached.set()
        await release.wait()
        return CameraCaptureResult(frame=b"late-A", source="fresh")

    monkeypatch.setattr("backend.app.services.camera_runtime.capture", capture)
    old_key = main._finish_photo_key(1)
    task = asyncio.create_task(main.on_finish_photo_moment(1, {"subtask_id": "80"}))
    await asyncio.wait_for(reached.wait(), timeout=5)
    old_event = main._stage22_finish_in_flight[old_key]
    bind_print_run(manager, printer_id=1, archive_id=81, observed_subtask_id="81")
    new_key = main._finish_photo_key(1)
    main._stage22_finish_frames[new_key] = b"B"
    release.set()
    await task
    try:
        assert old_event.is_set()
        assert old_key not in main._stage22_finish_frames
        assert main._stage22_finish_frames[new_key] == b"B"
    finally:
        main._stage22_finish_frames.pop(new_key, None)
        main._stage22_finish_in_flight.pop(old_key, None)


async def test_delayed_macro_uses_token_after_cleanup_and_rechecks_after_delay(monkeypatch):
    from backend.app.services import macro_trigger

    manager = PrinterManager()
    token = effect_token_for_run(bind_print_run(manager, printer_id=1, archive_id=80))
    discard_print_run(manager, 1, 80)
    reached = asyncio.Event()
    release = asyncio.Event()

    async def delay(_):
        reached.set()
        await release.wait()

    monkeypatch.setattr(macro_trigger.asyncio, "sleep", delay)
    dispatch = MagicMock(return_value=(True, None))
    monkeypatch.setattr(macro_trigger, "dispatch_mqtt_action", dispatch)
    macro = SimpleNamespace(
        delay_seconds=1, name="finish", action_type="mqtt_action", mqtt_action="light", mqtt_action_param="off"
    )
    task = asyncio.create_task(
        macro_trigger._run_one(macro, MagicMock(), may_run=lambda: print_effect_is_current(manager, token))
    )
    await asyncio.wait_for(reached.wait(), timeout=5)
    bind_print_run(manager, printer_id=1, archive_id=81)
    begin_print_run_finishing(manager, 1, 81)
    discard_print_run(manager, 1, 81)
    release.set()
    await task
    dispatch.assert_not_called()


async def test_completion_scope_releases_on_cancel():
    from backend.app.services.print_completion_tasks import PrintCompletionTasks, stop_print_completion_tasks

    scope = PrintCompletionTasks(42)
    cleanup = MagicMock()
    task = asyncio.create_task(asyncio.Event().wait())
    scope.add(task)
    scope.close(cleanup)
    cleanup.assert_not_called()
    await stop_print_completion_tasks(42)
    await asyncio.sleep(0)
    assert task.cancelled()
    cleanup.assert_called_once()


async def test_completion_scope_deadline_cancels_and_releases_once():
    from backend.app.services.print_completion_tasks import PrintCompletionTasks

    scope = PrintCompletionTasks(43, timeout=0)
    task = asyncio.create_task(asyncio.Event().wait())
    cleanup = MagicMock()
    scope.add(task)
    scope.close(cleanup)
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    cleanup.assert_called_once()


async def test_cancelled_stitch_reaps_ffmpeg_before_the_caller_unwinds(tmp_path, monkeypatch):
    """A cancelled finaliser is held until its encoder has really ended (G10 F).

    The encoder is a child ``media_child`` owns; a real process stands in for
    ffmpeg. Since G10 F the frames of a stitch that did not finish are KEPT for
    a recovery (they used to be removed here) — what must still hold is that no
    encoder outlives the finaliser that started it.
    """
    import sys

    from backend.app.services import layer_timelapse, media_child

    monkeypatch.setattr(layer_timelapse.settings, "base_dir", tmp_path)
    monkeypatch.setattr(layer_timelapse, "get_ffmpeg_path", lambda: "ffmpeg")
    real = media_child.run

    async def hanging(_argv, **kwargs):
        return await real([sys.executable, "-c", "import time; time.sleep(120)"], **kwargs)

    monkeypatch.setattr(layer_timelapse.media_child, "run", hanging)
    session = layer_timelapse.TimelapseSession(1, 80, "http://camera", "snapshot")
    (session.frames_dir / "layer_00001.jpg").write_bytes(b"frame")
    session.frame_count = 1
    task = asyncio.create_task(layer_timelapse.finish_session(session))
    for _ in range(200):
        if media_child.live():
            break
        await asyncio.sleep(0.05)
    assert media_child.live() == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert media_child.live() == 0  # reaped before the cancellation came through
    assert (session.frames_dir / "layer_00001.jpg").is_file()
    assert layer_timelapse._read_manifest(session.frames_dir)["state"] == layer_timelapse.STATE_RETAINED


async def test_detached_layer_session_failure_does_not_cancel_successor(tmp_path, monkeypatch):
    from backend.app.services import layer_timelapse

    monkeypatch.setattr(layer_timelapse.settings, "base_dir", tmp_path)
    monkeypatch.setattr(layer_timelapse, "_active_sessions", {})
    reached = asyncio.Event()
    release = asyncio.Event()

    async def stitch(_output, fps: int = 30):
        reached.set()
        await release.wait()
        raise RuntimeError("failed to stitch A")

    old = layer_timelapse.start_session(1, 80, "http://camera", "snapshot")
    (old.frames_dir / "layer_00001.jpg").write_bytes(b"frame")
    old.frame_count = 1
    monkeypatch.setattr(old, "stitch", stitch)
    detached = layer_timelapse.take_session(1, old)
    task = asyncio.create_task(layer_timelapse.finish_session(detached))
    await asyncio.wait_for(reached.wait(), timeout=5)
    new = layer_timelapse.start_session(1, 81, "http://camera", "snapshot")
    (new.frames_dir / "layer_00001.jpg").write_bytes(b"frame of the next print")
    release.set()
    assert await task is None
    # A's failure keeps A's frames (G10 F) and touches nothing of B.
    assert layer_timelapse._read_manifest(old.frames_dir)["state"] == layer_timelapse.STATE_RETAINED
    assert (new.frames_dir / "layer_00001.jpg").read_bytes() == b"frame of the next print"
    assert layer_timelapse._read_manifest(new.frames_dir)["state"] == layer_timelapse.STATE_ACTIVE
    assert layer_timelapse.get_session(1) is new


def test_no_archive_completion_is_once_per_live_generation():
    from backend.app.services.print_run_binding import (
        accept_unbound_completion,
        claim_print_start,
        finish_print_start,
        snapshot_completion_token,
    )

    manager = PrinterManager()
    admission = claim_print_start(manager, 1, {"subtask_id": "0"})
    finish_print_start(manager, 1, admission)
    token = snapshot_completion_token(manager, 1)
    assert accept_unbound_completion(manager, token)
    assert not accept_unbound_completion(manager, token)


def test_reprepared_same_archive_admits_new_start_without_old_cleanup_removing_it():
    from backend.app.services.print_run_binding import claim_print_start, finish_print_start, retire_print_start

    manager = PrinterManager()
    bind_print_run(manager, printer_id=1, archive_id=80, expected_submission_id="80")
    old = claim_print_start(manager, 1, {"subtask_id": "80"})
    finish_print_start(manager, 1, old)
    bind_print_run(manager, printer_id=1, archive_id=80, expected_submission_id="81")
    new = claim_print_start(manager, 1, {"subtask_id": "81"})
    assert new is not None
    retire_print_start(manager, 1, 80, expected=old)
    assert claim_print_start(manager, 1, {"subtask_id": "81"}) is None


@pytest.mark.parametrize("operation", ["delete", "rename", "patch"])
async def test_ftp_cleanup_rechecks_after_connect(operation, monkeypatch):
    from backend.app.services import bambu_ftp

    manager = PrinterManager()
    token = effect_token_for_run(bind_print_run(manager, printer_id=1, archive_id=80))
    reached = threading.Event()
    release = threading.Event()
    client = MagicMock()

    def connect():
        reached.set()
        assert release.wait(5)
        return True

    client.connect.side_effect = connect
    monkeypatch.setattr(bambu_ftp, "BambuFTPClient", MagicMock(return_value=client))
    kwargs = {"may_run": lambda: print_effect_is_current(manager, token)}
    if operation == "delete":
        callback = bambu_ftp.delete_file_async("test-cleanup", "secret", "/A.3mf", **kwargs)
    elif operation == "rename":
        callback = bambu_ftp.rename_file_async("test-cleanup", "secret", "/A.3mf", "/cache/A.3mf", **kwargs)
    else:
        callback = bambu_ftp.upload_bytes_async("test-cleanup", "secret", b"patched", "/A.bbl", **kwargs)
    task = asyncio.create_task(callback)
    try:
        assert await asyncio.to_thread(reached.wait, 5)
        bind_print_run(manager, printer_id=1, archive_id=81)
    finally:
        release.set()
    await task
    client.delete_file.assert_not_called()
    client.rename_file.assert_not_called()
    client.upload_bytes.assert_not_called()
    client.disconnect.assert_called_once()


async def test_copy_cleanup_rechecks_after_content_read(monkeypatch):
    from backend.app.services.printer_cleanup import remove_verified_copies

    manager = PrinterManager()
    token = effect_token_for_run(bind_print_run(manager, printer_id=1, archive_id=80))
    data = b"same file content"

    async def read(_):
        bind_print_run(manager, printer_id=1, archive_id=81)
        return data

    delete = AsyncMock()
    removed = await remove_verified_copies(
        entries=[{"name": "A.gcode.3mf"}],
        wanted={"A.gcode.3mf"},
        expected_hashes={hashlib.sha256(data).hexdigest()},
        read_bytes=read,
        delete=delete,
        label="test",
        may_run=lambda: print_effect_is_current(manager, token),
    )
    assert removed == 0
    delete.assert_not_awaited()


async def test_internal_cleanup_rechecks_after_listing():
    from backend.app.services.background_dispatch import delete_internal_by_name

    manager = PrinterManager()
    token = effect_token_for_run(bind_print_run(manager, printer_id=1, archive_id=80))

    async def listing(_):
        bind_print_run(manager, printer_id=1, archive_id=81)
        return [SimpleNamespace(name="A.3mf", path="/history/A.3mf")]

    transport = SimpleNamespace(list_files=listing, delete=AsyncMock())
    assert not await delete_internal_by_name(
        transport, "A.3mf", may_run=lambda: print_effect_is_current(manager, token)
    )
    transport.delete.assert_not_awaited()


async def test_internal_transport_rechecks_after_connect(monkeypatch):
    from backend.app.services.printer_files.base import DeleteResult
    from backend.app.services.printer_files.tunnel import TunnelTransport

    manager = PrinterManager()
    token = effect_token_for_run(bind_print_run(manager, printer_id=1, archive_id=80))
    client = SimpleNamespace(delete_files=AsyncMock())

    @asynccontextmanager
    async def connection():
        bind_print_run(manager, printer_id=1, archive_id=81)
        yield client

    transport = TunnelTransport(SimpleNamespace(ip_address="test-cleanup", access_code="secret"))
    monkeypatch.setattr(transport, "_client", connection)
    assert (
        await transport.delete("/A.3mf", may_run=lambda: print_effect_is_current(manager, token)) == DeleteResult.FAILED
    )
    client.delete_files.assert_not_awaited()
