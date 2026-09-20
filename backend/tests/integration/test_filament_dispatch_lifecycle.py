"""Drive both real runners through public owners with only device I/O mocked."""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.background_dispatch import BackgroundDispatchService, PrintDispatchJob
from backend.app.services.filament_policy_write import prepare_routing
from backend.app.services.filament_preflight import preflight_item
from backend.app.services.filament_routing import RoutingDeferred
from backend.app.services.print_scheduler import PrintScheduler
from backend.app.services.queue_counters import get_queue_terminal_counts
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", ["print_library_file", "reprint_archive"])
@pytest.mark.parametrize("owner", ["direct", "queue"])
@pytest.mark.parametrize(
    "change", ["upload", "preheat", "calibration", "source", "cancel", "reclaim", "delete", "success"]
)
async def test_late_refusal_restores_source_and_does_not_count_a_print(
    db_session,
    test_engine,
    tmp_path,
    printer_factory,
    monkeypatch,
    kind,
    owner,
    change,
):
    import backend.app.services.background_dispatch as bd

    source, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "archive_dir", tmp_path / "archives")
    settings.archive_dir.mkdir()
    source_archive = None
    if kind == "reprint_archive":
        source_archive = PrintArchive(
            printer_id=printer.id,
            filename=source.filename,
            file_path=source.file_path,
            file_size=source.file_size,
            status="completed",
            plate_index=15,
        )
        db_session.add(source_archive)
        await db_session.commit()
    archive_id = source_archive.id if source_archive else None
    library_id = None if source_archive else source.id
    routing, plate = await prepare_routing(
        db_session, printer_id=printer.id, archive_id=archive_id, library_file_id=library_id
    )
    item = PrintQueueItem(
        queue_id=queue.id,
        archive_id=archive_id,
        library_file_id=library_id,
        filament_routing=routing,
        plate_id=plate,
        status="printing",
        started_at=datetime.now(),
        origin=owner,
    )
    db_session.add(item)
    await db_session.flush()
    queue.current_item_id, queue.status = item.id, "printing"
    await db_session.commit()
    item_id, queue_id, printer_id = item.id, queue.id, printer.id
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(bd, "async_session", factory)
    monkeypatch.setattr("backend.app.services.print_scheduler.async_session", factory)
    monkeypatch.setattr(bd, "resolve_dispatch_storage", lambda *_: ("external", None))
    monkeypatch.setattr(bd, "delete_file_async", AsyncMock(return_value=True))
    monkeypatch.setattr(bd, "list_files_async", AsyncMock(return_value=[]))
    monkeypatch.setattr(bd, "get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 30)))
    monkeypatch.setattr(bd.printer_manager, "ensure_fresh_connection_for_printer", AsyncMock(return_value=True))
    monkeypatch.setattr(bd, "_warn_on_filament_deficit", AsyncMock())
    monkeypatch.setattr(bd.ws_manager, "broadcast", AsyncMock())
    register, withdraw, rollback = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr("backend.app.main.register_expected_print", register)
    monkeypatch.setattr("backend.app.main.withdraw_expected_print", withdraw)
    monkeypatch.setattr("backend.app.services.preheat.rollback", rollback)
    failure = AsyncMock()
    monkeypatch.setattr("backend.app.services.notification_service.notification_service.on_queue_job_failed", failure)

    def mutate():
        mqtt._process_message({"print": {"vt_tray": {"id": 254, "tray_type": "PETG"}}})

    async def upload(*args, **kwargs):
        if change == "upload":
            mutate()
        elif change == "source":
            with Path(source.file_path).open("ab") as stream:
                stream.write(b"source revision changed")
        return True

    async def preheat(*args, **kwargs):
        if change == "preheat":
            mutate()

    async def calibration(*args, **kwargs):
        if change == "calibration":
            mutate()
        elif change in {"cancel", "reclaim", "delete"}:
            from backend.app.models.printer_queue import PrinterQueue

            async with factory() as concurrent:
                claimed = await concurrent.get(PrintQueueItem, item_id)
                if change == "cancel":
                    claimed.status = "cancelled"
                elif change == "reclaim":
                    claimed.started_at += timedelta(seconds=1)
                else:
                    owned_queue = await concurrent.get(PrinterQueue, queue_id)
                    owned_queue.current_item_id, owned_queue.status = None, "idle"
                    await concurrent.delete(claimed)
                await concurrent.commit()

    upload_mock = AsyncMock(side_effect=upload)
    monkeypatch.setattr(bd, "upload_file_async", upload_mock)
    monkeypatch.setattr("backend.app.services.preheat.preheat_and_soak", AsyncMock(side_effect=preheat))
    monkeypatch.setattr(bd, "_apply_calibrations_for_print", AsyncMock(side_effect=calibration))
    service = BackgroundDispatchService()
    monkeypatch.setattr(service, "_ensure_live_connection_before_start", AsyncMock())
    monkeypatch.setattr(service, "_run_swap_macro_if_needed", AsyncMock())
    monkeypatch.setattr(service, "_verify_print_response", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "_strict_stagger_refuses", AsyncMock(return_value=False))
    monkeypatch.setattr("backend.app.services.print_scheduler.scheduler.acquire_stagger_slot", AsyncMock())
    options = {"plate_id": plate, "mesh_mode_fast_check": True}
    if owner == "queue":
        monkeypatch.setattr(bd, "background_dispatch", service)
        await PrintScheduler()._dispatch_and_finalize(
            queue_item_id=item_id,
            printer_id=printer_id,
            printer_name=printer.name,
            printer_serial=printer.serial_number,
            dispatch_kind=kind,
            dispatch_source_id=archive_id or library_id,
            dispatch_source_name=source.filename,
            options=options,
            requested_by_user_id=None,
            project_id=None,
            project_line_id=None,
            job_name_short="part",
            swap_events=[],
        )
    else:
        job = PrintDispatchJob(
            id=1,
            kind=kind,
            source_id=archive_id or library_id,
            source_name=source.filename,
            printer_id=printer_id,
            printer_name=printer.name,
            options=options,
            queue_item_id=item_id,
            cleanup_library_after_dispatch=True,
        )
        await service._run_active_job(job)
        assert job.completion_event.is_set()
        assert bool(job.outcome.get("deferred")) == (change != "success"), job.outcome
        assert service._batch_failed == 0
        assert service._batch_completed == 0  # No UI batch is registered by this direct wrapper harness.
        if change == "success":
            assert job.outcome["success"] is True
    if change == "success":
        import json

        from backend.app.services.filament_policy import restore_routing_source

        await db_session.refresh(item)
        assert item.status == "printing"
        mqtt._client.publish.assert_called_once()
        command = json.loads(mqtt._client.publish.call_args.args[1])["print"]
        assert command["use_ams"] is False and command["param"] == "Metadata/plate_15.gcode"
        register.assert_called_once()
        assert register.call_args.kwargs["ams_mapping"] == [-1, -1, 254]
        withdraw.assert_not_called()
        failure.assert_not_awaited()
        if owner == "direct" and kind == "print_library_file":
            saved = json.loads(item.filament_routing)
            assert saved["source_identity"]["kind"] == "archive"
            assert saved["source_identity"]["id"] == item.archive_id
            restore_routing_source(item)
            assert item.library_file_id is None
            assert (await preflight_item(db_session, item, printer_id)).plan.mapping == [-1, -1, 254]
        return
    if change == "delete":
        db_session.expunge(item)
        assert await db_session.get(PrintQueueItem, item_id) is None
    else:
        await db_session.refresh(item)
    if change in {"cancel", "reclaim"}:
        assert item.status == ("cancelled" if change == "cancel" else "printing")
    elif change != "delete":
        assert item.status == ("pending" if owner == "queue" else "cancelled"), item.error_message
        assert (item.archive_id, item.library_file_id) == (archive_id, library_id)
    rows = (await db_session.execute(select(PrintArchive).order_by(PrintArchive.id))).scalars().all()
    execution = [a for a in rows if a.id != archive_id]
    assert len(execution) == 1
    assert execution[0].status == "cancelled" and execution[0].extra_data["dispatch_aborted"] is True
    assert execution[0].plate_index == 15
    if source_archive:
        await db_session.refresh(source_archive)
        assert source_archive.status == "completed"
    assert (await get_queue_terminal_counts(db_session, queue_id))["total_count"] == 0
    assert Path(source.file_path).exists()
    assert upload_mock.await_count == 1
    mqtt._client.publish.assert_not_called()
    failure.assert_not_awaited()
    register.assert_called_once()
    assert register.call_args.kwargs["ams_mapping"] == [-1, -1, 254]
    withdraw.assert_called_once()
    if change == "reclaim":
        rollback.assert_not_called()
    else:
        rollback.assert_called_with(printer_id)
    if owner == "queue" and change not in {"cancel", "reclaim", "delete"}:
        with pytest.raises(RoutingDeferred):
            await preflight_item(db_session, item, printer_id)
