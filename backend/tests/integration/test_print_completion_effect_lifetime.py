"""Exercise the public callbacks, rather than only their inner handlers."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import make_transient

from backend.app import main
from backend.app.services.print_run_binding import bind_prepared_print_run
from backend.app.services.printer_manager import PrinterManager


async def test_actual_completion_backgrounds_run_after_callback(
    test_engine, db_session, printer_factory, archive_factory, tmp_path, monkeypatch
):
    from datetime import datetime, timezone

    from backend.app.core.database import Base
    from backend.app.core.tasks import spawn_background_task
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.models.settings import Settings
    from backend.app.services.print_run_binding import bind_print_run, finishing_print_runs

    printer = await printer_factory(auto_archive=True, plate_detection_enabled=False)
    archive = await archive_factory(printer.id, status="printing", subtask_id="81", file_path="")
    queue = PrinterQueue(id=100, printer_id=printer.id)
    db_session.add(queue)
    await db_session.flush()
    row = PrintQueueItem(
        queue_id=queue.id, archive_id=archive.id, status="printing", started_at=datetime.now(timezone.utc)
    )
    setting = Settings(key="capture_finish_photo", value="true")
    db_session.add_all([row, setting])
    await db_session.commit()
    # The normal in-memory fixture shares one SQLite connection. Real
    # concurrent completion sessions require independent file connections.
    file_engine = None
    if test_engine.dialect.name == "sqlite":
        file_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.db'}")
        async with file_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(file_engine, expire_on_commit=False)
        for obj in (printer, archive, queue, row, setting):
            db_session.expunge(obj)
            make_transient(obj)
        async with factory() as db:
            db.add_all([printer, archive, queue, row, setting])
            await db.commit()
    else:
        factory = async_sessionmaker(test_engine, expire_on_commit=False)
    manager = PrinterManager()
    bind_print_run(
        manager,
        printer_id=printer.id,
        archive_id=archive.id,
        queue_item_id=row.id,
        claim_started_at=row.started_at,
        observed_subtask_id="81",
        archive_identity=archive.recovery_identity,
    )
    monkeypatch.setattr(main, "printer_manager", manager)
    monkeypatch.setattr(main, "async_session", factory)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    monkeypatch.setattr(main.app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(main, "ws_manager", AsyncMock())
    monkeypatch.setattr(main, "mqtt_relay", AsyncMock())
    plugs = AsyncMock()
    monkeypatch.setattr(main, "smart_plug_manager", plugs)
    notifications = AsyncMock()
    monkeypatch.setattr(main, "notification_service", notifications)
    energy = AsyncMock()
    monkeypatch.setattr(main, "_record_print_energy", energy)
    monkeypatch.setattr(main, "_record_terminal_effect_stage", AsyncMock())
    monkeypatch.setattr(
        "backend.app.services.archive_download_retry.archive_download_retry.retry_archive",
        AsyncMock(return_value="failed"),
    )
    monkeypatch.setattr("backend.app.services.macro_trigger.fire_event_macros", AsyncMock())
    monkeypatch.setattr("backend.app.services.usage_tracker.on_print_complete", AsyncMock(return_value=[]))
    photo_key = main._finish_photo_key(printer.id)
    main._stage22_finish_frames[photo_key] = b"captured-frame-A"
    event = asyncio.Event()
    event.set()
    main._stage22_finish_in_flight[photo_key] = event
    release = asyncio.Event()
    tasks = []

    def schedule(coro, *, name):
        if not name.startswith("finish-"):
            return spawn_background_task(coro, name=name)

        async def after_callback():
            try:
                await release.wait()
                return await coro
            finally:
                coro.close()

        task = spawn_background_task(after_callback(), name=name)
        tasks.append(task)
        return task

    monkeypatch.setattr(main, "spawn_background_task", schedule)
    try:
        await main.on_print_complete(printer.id, {"status": "completed", "subtask_id": "81"})
        assert tasks
        assert finishing_print_runs(manager, printer.id)
        release.set()
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)  # Task done callbacks release the scope.
        energy.assert_awaited_once()
        plugs.on_print_complete.assert_awaited_once()
        assert plugs.on_print_complete.call_args.kwargs["may_run"]()
        notifications.on_print_complete.assert_awaited_once()
        assert not finishing_print_runs(manager, printer.id)
        await main.on_print_complete(printer.id, {"status": "completed", "subtask_id": "81"})
        main.ws_manager.send_print_complete.assert_awaited_once()
        energy.assert_awaited_once()
        plugs.on_print_complete.assert_awaited_once()
        notifications.on_print_complete.assert_awaited_once()
        async with factory() as db:
            stored = await db.get(PrintArchive, archive.id)
            assert stored.photos and stored.photos[0].startswith("finish_")
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        main._stage22_finish_frames.pop(photo_key, None)
        main._stage22_finish_in_flight.pop(photo_key, None)
        if file_engine is not None:
            await file_engine.dispose()


@pytest.mark.parametrize("failed_effect", [None, "websocket", "spool_assignment"])
async def test_prepared_dispatch_first_start_notifies_once(
    test_engine, printer_factory, archive_factory, monkeypatch, failed_effect
):
    printer = await printer_factory(auto_archive=True, plate_detection_enabled=False)
    archive = await archive_factory(printer.id, status="printing", subtask_id="1171250830")
    manager = PrinterManager()
    monkeypatch.setattr(main, "printer_manager", manager)
    monkeypatch.setattr(main, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    ws = AsyncMock()
    monkeypatch.setattr(main, "ws_manager", ws)
    monkeypatch.setattr(main, "mqtt_relay", AsyncMock())
    monkeypatch.setattr(main, "smart_plug_manager", AsyncMock())
    monkeypatch.setattr(main, "notify_missing_spool_assignments_on_print_start", AsyncMock())
    if failed_effect == "websocket":
        ws.send_print_start.side_effect = RuntimeError("unavailable WebSocket")
    elif failed_effect == "spool_assignment":
        main.notify_missing_spool_assignments_on_print_start.side_effect = RuntimeError("unavailable spool notice")
    monkeypatch.setattr("backend.app.services.usage_tracker.on_print_start", AsyncMock())
    monkeypatch.setattr("backend.app.services.macro_trigger.fire_event_macros", AsyncMock())
    monkeypatch.setattr(main, "_load_objects_from_archive", MagicMock())
    monkeypatch.setattr(main, "_record_energy_start", AsyncMock())
    monkeypatch.setattr(main, "_store_spoolman_print_data", AsyncMock())
    monkeypatch.setattr(main, "_capture_timelapse_baseline_at_start", AsyncMock())
    notify = AsyncMock()
    monkeypatch.setattr(main, "_send_print_start_notification", notify)
    bind_prepared_print_run(manager, printer_id=printer.id, archive_id=archive.id, expected_submission_id="1171250830")
    main.register_expected_print(printer.id, archive.filename, archive.id)
    data = {"filename": archive.filename, "subtask_name": archive.print_name, "subtask_id": "1171250830"}
    try:
        await main.on_print_start(printer.id, data)
        await main.on_print_start(printer.id, data)
        notify.assert_awaited_once()
        ws.send_print_start.assert_awaited_once()
    finally:
        for mapping in (main._active_prints, main._expected_prints, main._expected_print_registered_at):
            for key in list(mapping):
                if key[0] == printer.id:
                    mapping.pop(key, None)


async def test_concurrent_no_archive_starts_share_admission(test_engine, printer_factory, monkeypatch):
    printer = await printer_factory(auto_archive=False, plate_detection_enabled=False)
    monkeypatch.setattr(main, "printer_manager", PrinterManager())
    monkeypatch.setattr(main, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    ws = AsyncMock()
    monkeypatch.setattr(main, "ws_manager", ws)
    monkeypatch.setattr(main, "mqtt_relay", AsyncMock())
    monkeypatch.setattr(main, "smart_plug_manager", AsyncMock())
    monkeypatch.setattr(main, "notify_missing_spool_assignments_on_print_start", AsyncMock())
    monkeypatch.setattr("backend.app.services.usage_tracker.on_print_start", AsyncMock())
    monkeypatch.setattr("backend.app.services.macro_trigger.fire_event_macros", AsyncMock())
    notify = AsyncMock()
    monkeypatch.setattr(main, "_send_print_start_notification", notify)
    await asyncio.gather(*(main.on_print_start(printer.id, {"subtask_id": "0"}) for _ in range(2)))
    notify.assert_awaited_once()
    ws.send_print_start.assert_awaited_once()


async def test_unarchived_successor_can_finish_while_old_archive_consumers_remain(monkeypatch, test_engine):
    from backend.app.services.print_run_binding import (
        begin_print_run_finishing,
        bind_print_run,
        claim_print_start,
        finish_print_start,
    )

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(main, "async_session", factory)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    manager = PrinterManager()
    bind_print_run(manager, printer_id=1, archive_id=80, observed_subtask_id="80")
    begin_print_run_finishing(manager, 1, 80)
    start = claim_print_start(manager, 1, {"subtask_id": "81"})
    finish_print_start(manager, 1, start)
    monkeypatch.setattr(main, "printer_manager", manager)
    monkeypatch.setattr(main, "_completion_conflicts_with_active_queue", AsyncMock(return_value=False))
    monkeypatch.setattr(main, "ws_manager", AsyncMock())
    monkeypatch.setattr(main, "mqtt_relay", AsyncMock())
    await main.on_print_complete(1, {"status": "completed", "subtask_id": "80"})
    assert not getattr(manager, "_print_terminal_generations", {})
    await main.on_print_complete(1, {"status": "completed", "subtask_id": "81"})
    assert manager._print_terminal_generations[1] == start.generation
