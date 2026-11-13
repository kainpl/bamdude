"""F05: actual VP library commit and source/queue publication survive drain."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.core import database
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.queue_source import QueueSource
from backend.app.services import queue_sources
from backend.app.services.farm_startup_guard import farm_startup_guard
from backend.app.services.virtual_printer.lifecycle import VPDrainRefused, drain_warning
from backend.app.services.virtual_printer.manager import VirtualPrinterInstance, VirtualPrinterManager
from backend.tests.integration.test_filament_routing_dispatch import setup_source
from backend.tests.unit.services.test_vp_ftp_stor import _make_session


@pytest.mark.parametrize("mode,model", [("print_queue", PrintQueueItem), ("auto_queue", AutoQueueItem)])
@pytest.mark.parametrize("g01_closed", [False, True])
async def test_226_then_real_library_commit_then_real_queue_publish_during_drain(
    test_engine, db_session, tmp_path, printer_factory, monkeypatch, mode, model, g01_closed
):
    monkeypatch.setattr(farm_startup_guard, "restart_required", False)
    source, printer, queue, _ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", sessions)
    queue_sources._reset_state()
    vp = VirtualPrinterInstance(
        vp_id=7,
        name="Owned",
        mode=mode,
        model="C11",
        access_code="12345678",
        serial_suffix="12345678",
        base_dir=tmp_path,
        session_factory=sessions,
        target_printer_id=printer.id,
    )
    monkeypatch.setattr(vp, "_find_best_queue", AsyncMock(return_value=queue))
    library_committed, release = asyncio.Event(), asyncio.Event()
    real_save = vp._save_to_library
    saved = []

    async def save(*args):
        row = await real_save(*args)
        assert row is not None
        saved.append(row.id)
        library_committed.set()
        await release.wait()
        return row

    monkeypatch.setattr(vp, "_save_to_library", save)
    session = _make_session(tmp_path, data_chunks=[source_path_bytes(source)])
    session.activities = vp.activities
    session.on_file_received = vp.on_file_received
    replies = []
    session.send = AsyncMock(side_effect=lambda code, _: replies.append(code))
    uploading = asyncio.create_task(session.cmd_STOR("send.3mf"))
    await asyncio.wait_for(library_committed.wait(), 10)
    assert 226 in replies  # already acknowledged before the barrier
    async with sessions() as db:
        assert await db.get(LibraryFile, saved[0]) is not None
        assert (await db.execute(select(model))).scalars().all() == []
    if g01_closed:
        from backend.app.core.auth_runtime import auth_runtime
        from backend.app.services import cloud_credentials, library_scan
        from backend.app.services.slice_dispatch import slice_dispatch

        monkeypatch.setattr(library_scan, "_admitting", library_scan._admitting)
        monkeypatch.setattr(slice_dispatch, "open", slice_dispatch.open)
        auth_runtime.close_admission()
        cloud_credentials.close_admission()
        slice_dispatch.close_admission()
        library_scan.close_admission()
        assert not auth_runtime.admitting
    manager = VirtualPrinterManager()
    manager._instances[vp.id] = vp
    manager.drain_timeout = 0.02
    with pytest.raises(VPDrainRefused):
        await manager.drain_for_restore()
    assert not uploading.done()
    release.set()
    await asyncio.wait_for(uploading, 15)
    manager.drain_timeout = 5
    outcome = await manager.drain_for_restore()
    async with sessions() as db:
        rows = (await db.execute(select(model))).scalars().all()
        assert rows and all(row.library_file_id == saved[0] for row in rows)
        assert all(row.queue_source_id is not None for row in rows)
        assert (await db.execute(select(QueueSource))).scalar_one() is not None
    assert outcome["uploads_without_options"] == 1
    assert "Resending can duplicate" in drain_warning(outcome, git=True)
    queue_sources._reset_state()


def source_path_bytes(source):
    from pathlib import Path

    return Path(source.file_path).read_bytes()
