"""G01 E authority/owned export composes with the shared G02 restore guard."""

import asyncio
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile

from backend.app.api.routes import settings as routes
from backend.app.models.auth_session import AuthSession
from backend.app.models.git_backup import GitBackupLog
from backend.app.schemas.git_backup import RestoreCategory
from backend.app.services import git_backup, virtual_printer
from backend.app.services.farm_startup_guard import farm_startup_guard
from backend.app.services.git_restore import GitRestoreService
from backend.tests.integration.test_g01_e_backup_approval import backup  # noqa: F401
from backend.tests.unit.test_g01_e_git_restore_actor import actor_restore  # noqa: F401


async def test_admitted_scheduled_export_is_joined_without_cancel_and_excludes_restores(
    backup, db_session, monkeypatch
):
    service, factory, (cid, _, _) = backup
    monkeypatch.setattr(git_backup, "git_backup_service", service)
    entered, release = asyncio.Event(), asyncio.Event()

    async def push(*args):
        entered.set()
        await release.wait()
        return {"status": "success", "commit_sha": "a" * 40}

    service._push_to_provider = push
    exporting = asyncio.create_task(service.run_backup(cid, "scheduled"))
    service._scheduler_task = exporting
    try:
        await asyncio.wait_for(entered.wait(), 3)
        # Real ZIP entry refuses on the same guard before parsing or closing admission.
        with pytest.raises(HTTPException) as refused:
            await asyncio.wait_for(
                routes.restore_backup(UploadFile(BytesIO(b"not parsed"), filename="backup.zip"), db_session), 2
            )
        assert refused.value.status_code == 409
        restore = GitRestoreService()
        restore._resolve_ref = AsyncMock()
        result = await asyncio.wait_for(restore.run_restore(cid, "HEAD", [RestoreCategory.SETTINGS]), 2)
        assert not result["success"]
        restore._resolve_ref.assert_not_awaited()
        service.close_admission()
        assert not await service.quiesce_for_restore(0.01)
        assert not exporting.done() and not exporting.cancelling()
        release.set()
        result = await asyncio.wait_for(exporting, 3)
        assert result["success"]
        assert await service.quiesce_for_restore(1)
        async with factory() as db:
            log = await db.get(GitBackupLog, result["log_id"])
            assert log.status == "success" and log.scheduled_for is not None
        assert not (await service.run_backup(cid))["success"]
    finally:
        release.set()
        await asyncio.gather(exporting, return_exceptions=True)


@pytest.mark.parametrize("revoke", [False, True])
async def test_git_restore_preserves_actor_through_guard_and_has_no_transaction_during_vp_proof(
    actor_restore, monkeypatch, revoke
):
    factory, sessions, capture, cid = actor_restore
    restore = GitRestoreService()
    restore._resolve_ref = AsyncMock(return_value=("a" * 40, "", None))
    restore._read_categories = AsyncMock(return_value=({}, ""))
    entered = False

    async def drain():
        nonlocal entered
        entered = True
        assert farm_startup_guard.restart_required
        assert not sessions[-1].in_transaction()
        # A different Git service is also excluded by the shared operation guard.
        blocked = git_backup.GitBackupService()
        blocked._collect_backup_data = AsyncMock()
        result = await asyncio.wait_for(blocked.run_backup(cid), 2)
        assert not result["success"] and result["log_id"] is None
        blocked._collect_backup_data.assert_not_awaited()
        if revoke:
            from datetime import datetime

            async with factory() as db:
                (await db.get(AuthSession, capture.sid)).revoked_at = datetime.utcnow()
                await db.commit()
        return {"proxy_connections_cut": {}, "camera_leases_released": 0, "uploads_without_options": 0}

    monkeypatch.setattr(virtual_printer.virtual_printer_manager, "drain_for_restore", drain)
    if revoke:
        with pytest.raises(HTTPException) as refused:
            await restore.run_restore(cid, "HEAD", [RestoreCategory.SETTINGS], authority=capture)
        assert refused.value.status_code == 401
    else:
        result = await restore.run_restore(cid, "HEAD", [RestoreCategory.SETTINGS], authority=capture)
        assert result["success"]
    assert entered
