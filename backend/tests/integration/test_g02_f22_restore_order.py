"""Merged G01 admission closes synchronously; accepted VP work drains before joins."""

import asyncio
import threading
from io import BytesIO
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select

from backend.app.api.routes import settings as routes
from backend.app.core import db_portable
from backend.app.core.auth_runtime import auth_runtime
from backend.app.services import (
    backup_files,
    cloud_credentials,
    farm_startup_guard as farm_module,
    library_scan,
    obico_history_files,
    virtual_printer,
)
from backend.app.services.credential_rollout import credential_rollout
from backend.app.services.farm_startup_guard import farm_startup_guard
from backend.app.services.git_backup import GitBackupService
from backend.app.services.slice_dispatch import slice_dispatch
from backend.app.services.virtual_printer.manager import VirtualPrinterInstance, VirtualPrinterManager


@pytest.fixture(autouse=True)
def isolated_git_backup(monkeypatch):
    from backend.app.services import git_backup

    service = GitBackupService()
    monkeypatch.setattr(git_backup, "git_backup_service", service)
    return service


@pytest.mark.parametrize("timeout", [False, True])
async def test_zip_closes_g01_admission_then_proves_vp_before_any_other_join(
    db_session, monkeypatch, tmp_path, timeout, isolated_git_backup
):
    class Files:
        committed = False

        def __init__(self, *args):
            pass

        def finish(self):
            pass

    monkeypatch.setattr(backup_files, "FileRestore", Files)
    monkeypatch.setattr(db_portable, "validate_sqlite_backup", lambda *args, **kwargs: None)
    monkeypatch.setattr(backup_files, "validate_staged_queue_spool", lambda *args: None)
    monkeypatch.setattr(obico_history_files, "validate_history", lambda *args: None)
    events = []
    for owner, label in [
        (auth_runtime, "auth"),
        (cloud_credentials, "cloud"),
        (slice_dispatch, "slice"),
        (library_scan, "scan"),
        (isolated_git_backup, "git-backup"),
    ]:
        real_close = owner.close_admission

        def close(real_close=real_close, label=label):
            events.append(label)
            real_close()

        monkeypatch.setattr(owner, "close_admission", close)
    monkeypatch.setattr(library_scan, "_admitting", True)
    monkeypatch.setattr(slice_dispatch, "open", slice_dispatch.open)
    monkeypatch.setattr(farm_startup_guard, "active", False)

    async def notify():
        assert farm_startup_guard.restart_required
        assert events == ["auth", "cloud", "slice", "scan", "git-backup"]
        events.append("notified")

    monkeypatch.setattr(farm_module, "notify_change", notify)
    manager = VirtualPrinterManager()
    vp = VirtualPrinterInstance(
        vp_id=7,
        name="Owned",
        mode="file_manager",
        model="N2S",
        access_code="12345678",
        serial_suffix="12345678",
        base_dir=tmp_path,
    )
    manager._instances[vp.id] = vp
    manager.drain_timeout = 0.1 if timeout else 5
    monkeypatch.setattr(virtual_printer, "virtual_printer_manager", manager)
    entered, release, draining = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def accepted_upload():
        with vp.activities.business():
            entered.set()
            await release.wait()
            events.append("published")

    uploading = asyncio.create_task(accepted_upload())
    await asyncio.wait_for(entered.wait(), 2)
    real_drain = manager.drain_for_restore

    async def drain():
        assert events == ["auth", "cloud", "slice", "scan", "git-backup", "notified"]
        assert isolated_git_backup._closed
        assert farm_startup_guard.restart_required
        assert not auth_runtime.admitting
        assert not db_session.in_transaction()
        # A fresh service cannot bypass ZIP's shared operation guard, even
        # though it has not received this singleton's admission closure.
        fresh_backup = GitBackupService()
        fresh_backup._run_backup_owned = AsyncMock(side_effect=AssertionError("ZIP lost its guard"))
        refused = await fresh_backup.run_backup(1)
        assert not refused["success"] and refused["log_id"] is None
        fresh_backup._run_backup_owned.assert_not_awaited()
        events.append("vp")
        draining.set()
        return await real_drain()

    monkeypatch.setattr(manager, "drain_for_restore", drain)

    async def first_g01_join():
        assert uploading.done() and events[-1] == "published"
        events.append("g01-join")
        # The real route stops before unrelated owners or destructive storage work.
        raise HTTPException(409, "F22 boundary reached")

    monkeypatch.setattr(isolated_git_backup, "quiesce_for_restore", first_g01_join)
    # A refusal must not advance into any other owner's join.
    later = AsyncMock(side_effect=AssertionError("joined before VP proof"))
    monkeypatch.setattr(slice_dispatch, "quiesce_for_restore", later)
    monkeypatch.setattr(library_scan, "quiesce_for_restore", later)
    monkeypatch.setattr(credential_rollout, "quiesce_for_restore", later)
    monkeypatch.setattr(routes, "_quiesce_library_previews", later)
    body = BytesIO()
    with ZipFile(body, "w") as archive:
        archive.writestr("bamdude.db", b"only preflight is stubbed")
    await db_session.scalar(select(1))
    assert db_session.in_transaction()
    restoring = asyncio.create_task(
        routes.restore_backup(UploadFile(BytesIO(body.getvalue()), filename="backup.zip"), db_session)
    )
    try:
        await asyncio.wait_for(draining.wait(), 3)
        assert not uploading.done()
        assert "g01-join" not in events
        if not timeout:
            release.set()
            await asyncio.wait_for(uploading, 3)
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(restoring, 3)
        assert error.value.status_code == 409
        if timeout:
            assert "F22 boundary" not in error.value.detail
            assert not uploading.done() and "g01-join" not in events
        else:
            assert error.value.detail == "F22 boundary reached"
        later.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(uploading, restoring, return_exceptions=True)
        manager.drain_timeout = 3
        await real_drain()


@pytest.mark.parametrize("assembly_proven", [False, True])
async def test_zip_keeps_real_upload_thread_fence_after_vp_proof(db_session, monkeypatch, tmp_path, assembly_proven):
    from backend.app.mcp_server.runtime import mcp_runtime
    from backend.app.services import library_file_runtime, library_preview_jobs
    from backend.app.services.assembly import runtime as assembly_runtime
    from backend.app.services.library_file_runtime import UploadWriteOwner

    class Files:
        committed = False

        def __init__(self, *args):
            pass

        def finish(self):
            pass

    monkeypatch.setattr(backup_files, "FileRestore", Files)
    monkeypatch.setattr(db_portable, "validate_sqlite_backup", lambda *args, **kwargs: None)
    monkeypatch.setattr(backup_files, "validate_staged_queue_spool", lambda *args: None)
    monkeypatch.setattr(obico_history_files, "validate_history", lambda *args: None)
    monkeypatch.setattr(farm_startup_guard, "active", False)
    monkeypatch.setattr(farm_module, "notify_change", AsyncMock())
    monkeypatch.setattr(slice_dispatch, "open", slice_dispatch.open)
    monkeypatch.setattr(library_scan, "_admitting", library_scan._admitting)
    for runtime in (slice_dispatch, library_scan, credential_rollout, auth_runtime):
        monkeypatch.setattr(runtime, "quiesce_for_restore", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "_quiesce_part_render", AsyncMock())
    later = AsyncMock(return_value=False)
    monkeypatch.setattr(mcp_runtime, "quiesce_for_restore", later)
    preview = AsyncMock(side_effect=AssertionError("preview cannot precede the upload fence"))
    monkeypatch.setattr(library_preview_jobs, "quiesce_library_previews_for_restore", preview)
    manager = VirtualPrinterManager()
    monkeypatch.setattr(virtual_printer, "virtual_printer_manager", manager)

    def assembly_proof():
        assert manager._drain_task.done()
        slice_dispatch.quiesce_for_restore.assert_awaited_once()
        return assembly_proven

    monkeypatch.setattr(assembly_runtime, "assembly_quiesced", assembly_proof)
    owner = UploadWriteOwner()
    monkeypatch.setattr(library_file_runtime, "upload_writes", owner)
    real_quiesce = owner.quiesce

    async def short_quiesce():
        assert manager._drain_task.done()
        return await real_quiesce(0.05)

    monkeypatch.setattr(owner, "quiesce", short_quiesce)
    entered, release = threading.Event(), threading.Event()
    target = tmp_path / "raw-upload"

    def write():
        entered.set()
        assert release.wait(10)
        target.write_bytes(b"finished")

    writing = asyncio.create_task(owner.run(write))
    body = BytesIO()
    with ZipFile(body, "w") as archive:
        archive.writestr("bamdude.db", b"only preflight is stubbed")
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(HTTPException) as error:
            await routes.restore_backup(UploadFile(BytesIO(body.getvalue()), filename="backup.zip"), db_session)
        assert error.value.status_code == 409
        expected = (
            "Library upload writers have not stopped; retry restore after they finish"
            if assembly_proven
            else "Project assembly work has not stopped; restart before restoring"
        )
        assert error.value.detail == expected
        assert owner.closed is assembly_proven
        assert not writing.done() and len(owner.tasks) == 1
        later.assert_not_awaited()
        preview.assert_not_awaited()
    finally:
        release.set()
        await writing
        assert await real_quiesce(1)
    assert target.read_bytes() == b"finished"
