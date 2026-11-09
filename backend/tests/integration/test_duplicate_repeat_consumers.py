"""Review-r1 regressions for plate Repeat and delayed archive consumers."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue

pytestmark = [pytest.mark.asyncio]


async def _held_print(db, printer_factory, archive_factory):
    printer = await printer_factory(require_plate_clear=True)
    queue = PrinterQueue(printer_id=printer.id, status="paused", is_paused=True)
    db.add(queue)
    await db.flush()
    start = datetime.now(timezone.utc) - timedelta(hours=2)
    archive = await archive_factory(
        printer.id, queue_id=queue.id, status="completed", started_at=start, completed_at=start + timedelta(hours=1)
    )
    row = await db.get(Printer, printer.id)
    row.awaiting_plate_clear = True
    row.awaiting_plate_clear_archive_id = archive.id
    library = LibraryFile(filename="repeat.3mf", file_path="library/repeat.3mf", file_type="gcode", file_size=1)
    db.add(library)
    await db.commit()
    return printer, queue, archive, library


@pytest.mark.parametrize("code", ["operator_cancelled_stale_claim", "noncurrent_execution_claim"])
async def test_plate_repeat_never_rearms_a_stale_claim(db_session, printer_factory, archive_factory, code):
    """Fifth repeat door: plate-gate Repeat (web card + Telegram) re-arms the stale row itself."""
    from backend.app.services import plate_hold

    printer, queue, archive, library = await _held_print(db_session, printer_factory, archive_factory)
    stale = PrintQueueItem(
        queue_id=queue.id,
        archive_id=archive.id,
        library_file_id=library.id,
        status="cancelled",
        error_code=code,
        started_at=archive.started_at,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(stale)
    await db_session.commit()

    offered = await plate_hold.has_waiting_row(db_session, printer.id)
    try:
        row = await plate_hold.answer_by_repeating(db_session, printer.id, expected_archive_id=archive.id)
    except Exception:  # noqa: BLE001 - any refusal satisfies the contract
        row = None
    await db_session.refresh(stale)
    assert not offered and row is None and stale.status == "cancelled", (
        f"offered={offered} rearmed={row is not None} status={stale.status} code={stale.error_code} "
        f"archive_id={stale.archive_id} started_at={stale.started_at}"
    )


async def test_operator_cancel_makes_repeat_pick_the_stale_row_over_the_real_one(
    db_session, printer_factory, archive_factory
):
    """Vlad-shaped group: Q1 completed (the real held row) + E1 cancelled later as stale.

    waiting_row orders by completed_at DESC, so the operator's cancellation time makes E1 the row
    Repeat re-arms."""
    from backend.app.services import plate_hold

    printer, queue, archive, library = await _held_print(db_session, printer_factory, archive_factory)
    real = PrintQueueItem(
        queue_id=queue.id,
        archive_id=archive.id,
        library_file_id=library.id,
        status="completed",
        started_at=archive.started_at,
        completed_at=archive.completed_at,
    )
    stale = PrintQueueItem(
        queue_id=queue.id,
        archive_id=archive.id,
        library_file_id=library.id,
        status="cancelled",
        error_code="operator_cancelled_stale_claim",
        origin="external",
        started_at=archive.started_at,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add_all([real, stale])
    await db_session.commit()
    chosen = await plate_hold.waiting_row(db_session, printer.id)
    assert chosen is not None and chosen.id == real.id, (chosen.id if chosen else None, real.id, stale.id)


async def test_download_retry_never_marks_a_reissued_archive(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch
):
    """M12: archive_download_retry captures no incarnation before FTP; its marker lands on the new row."""
    from backend.app.services import archive_download_retry as retry_mod

    if test_engine.dialect.name != "sqlite":
        pytest.skip("actual SQLite id reissue")
    printer = await printer_factory()
    old = await archive_factory(printer.id, status="printing", file_path="")
    aid, identity = old.id, old.recovery_identity
    monkeypatch.setattr(retry_mod, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    replaced = []

    async def download(*_args, **_kwargs):
        await db_session.delete(old)
        await db_session.commit()
        new = await archive_factory(printer.id, status="printing", file_path="")
        assert new.id == aid and new.recovery_identity != identity
        replaced.append(new)
        return None  # the OLD print's download failed

    monkeypatch.setattr(retry_mod, "try_download_3mf", download)
    assert await retry_mod.archive_download_retry.retry_archive(aid) == "failed"
    new = replaced[0]
    await db_session.refresh(new)
    assert "no_3mf_available" not in (new.extra_data or {}), new.extra_data


async def test_terminal_effect_stage_never_lands_on_a_reissued_archive(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch
):
    """M12: delayed completion callbacks write bamdude_terminal_effects by numeric id only."""
    from backend.app import main
    from backend.app.services.print_state_recovery import runtime_effect_debt

    if test_engine.dialect.name != "sqlite":
        pytest.skip("actual SQLite id reissue")
    printer = await printer_factory()
    old = await archive_factory(printer.id, status="completed")
    aid, identity = old.id, old.recovery_identity
    await db_session.delete(old)
    await db_session.commit()
    new = await archive_factory(printer.id, status="completed")
    assert new.id == aid and new.recovery_identity != identity
    monkeypatch.setattr(main, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    # e.g. _post_print_printer_cleanup hitting its budget minutes after the old completion
    await main._record_terminal_effect_stage(aid, "printer_cleanup", "uncertain", expected_identity=identity)
    await db_session.refresh(new)
    assert "bamdude_terminal_effects" not in (new.extra_data or {}), (
        new.extra_data,
        runtime_effect_debt(new.extra_data, new.recovery_identity),
    )


async def test_terminal_effect_stage_still_records_the_addressed_archive(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch
):
    from backend.app import main

    printer = await printer_factory()
    archive = await archive_factory(printer.id, status="completed")
    monkeypatch.setattr(main, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    await main._record_terminal_effect_stage(
        archive.id, "printer_cleanup", "finished", expected_identity=archive.recovery_identity
    )
    await db_session.refresh(archive)
    assert archive.extra_data["bamdude_terminal_effects"]["printer_cleanup"]["stage"] == "finished"


@pytest.mark.parametrize("action", ["scan", "select"])
async def test_manual_timelapse_download_never_attaches_or_deletes_for_a_reissued_archive(
    async_client, db_session, test_engine, printer_factory, archive_factory, monkeypatch, action
):
    from unittest.mock import AsyncMock

    from backend.app.services import timelapse_files

    if test_engine.dialect.name != "sqlite":
        pytest.skip("actual SQLite id reissue")
    printer = await printer_factory(model="P1S")
    old = await archive_factory(printer.id, filename="timelapse-owner.3mf", status="completed")
    archive_id, identity = old.id, old.recovery_identity
    replaced = []
    video = {"name": "timelapse-owner.mp4", "path": "/timelapse/timelapse-owner.mp4"}
    monkeypatch.setattr(timelapse_files, "list_timelapse_videos", AsyncMock(return_value=([video], "sdcard")))
    remove = AsyncMock()
    monkeypatch.setattr(timelapse_files, "remove_recording_after_attach", remove)

    async def download(*args):
        await db_session.delete(old)
        await db_session.commit()
        new = await archive_factory(printer.id, status="completed")
        assert new.id == archive_id and new.recovery_identity != identity
        replaced.append(new)
        return b"the old recording"

    monkeypatch.setattr(timelapse_files, "read_timelapse_video", download)
    response = await async_client.post(
        f"/api/v1/archives/{archive_id}/timelapse/{action}", params={"filename": video["name"]}
    )
    assert response.status_code >= 400
    assert replaced
    await db_session.refresh(replaced[0])
    assert not replaced[0].timelapse_path
    remove.assert_not_awaited()


async def test_spoolman_snapshot_survives_duplicate_claims_without_choosing_options(
    db_session, printer_factory, archive_factory, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import select

    from backend.app.models.active_print_spoolman import ActivePrintSpoolman
    from backend.app.models.settings import Settings
    from backend.app.services import print_file_analysis
    from backend.app.services.spoolman_tracking import store_print_data

    printer, queue, archive, _ = await _held_print(db_session, printer_factory, archive_factory)
    db_session.add(Settings(key="spoolman_enabled", value="true"))
    for plate in (1, 2):
        db_session.add(PrintQueueItem(queue_id=queue.id, archive_id=archive.id, status="printing", plate_id=plate))
    await db_session.commit()
    usage = [{"id": 1, "used_g": 10}]
    analysis = AsyncMock(return_value=SimpleNamespace(filament_usage=usage, layer_usage={}, filament_properties={}))
    monkeypatch.setattr(print_file_analysis, "get_print_file_analysis", analysis)
    manager = MagicMock()
    manager.get_status.return_value = SimpleNamespace(raw_data={})
    await store_print_data(printer.id, archive.id, "archive/current.3mf", db_session, manager, plate_id=4)
    saved = (
        await db_session.scalars(select(ActivePrintSpoolman).where(ActivePrintSpoolman.archive_id == archive.id))
    ).one()
    assert saved.filament_usage == usage
    assert analysis.await_args.args[-1] == 4
