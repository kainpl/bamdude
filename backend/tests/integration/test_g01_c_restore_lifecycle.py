"""Restore publication is atomic; notice delivery never owns admission."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core import auth_lifetime, database
from backend.app.core.auth_runtime import AuthRuntime, auth_runtime
from backend.app.models.auth_session import AuthSession
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.services import (
    connector_runtime as connector_module,
    connector_submissions,
    farm_startup_guard as farm_module,
)
from backend.app.services.credential_rollout import CredentialRolloutRuntime
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("fault", ["auth", "epoch", "commit", "ambiguous", "unlink", None])
async def test_restore_joint_commit_and_recovery(test_engine, monkeypatch, tmp_path, fault):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    auth_runtime.open_admission()
    async with factory() as db:
        user = User(username="restored-owner", role="admin", is_active=True)
        db.add(user)
        await db.flush()
        issued = await auth_lifetime.stage_session(
            db, auth_lifetime.VerifiedOutcome(auth_lifetime.capture_account(user), "local"), remember_me=False
        )
        db.add(Settings(key=connector_submissions.STORAGE_EPOCH_KEY, value="old-epoch"))
        await db.commit()
        identity = user.account_identity
    runtime = connector_module.ConnectorRuntime()
    fence = tmp_path / "restore-fence"
    monkeypatch.setattr(connector_module, "fence_path", lambda: fence)
    runtime.write_restore_fence()
    assert not runtime.admitting

    async def failed(*args):
        raise RuntimeError("private detail must never escape")

    class FaultCommit(AsyncSession):
        async def commit(self):
            if fault == "ambiguous":
                await super().commit()
            raise RuntimeError("private commit detail")

    with monkeypatch.context() as failing:
        failing.setattr(database, "async_session", factory)
        if fault in ("commit", "ambiguous"):
            failing.setattr(database, "async_session", async_sessionmaker(test_engine, class_=FaultCommit))
        elif fault == "auth":
            failing.setattr(auth_lifetime, "stage_restore_invalidation", failed)
        elif fault == "epoch":
            failing.setattr(connector_submissions, "stage_storage_epoch", failed)
        elif fault == "unlink":
            original_unlink = Path.unlink

            def fail_fence(path, *args, **kwargs):
                if path == fence:
                    raise PermissionError("fence is held")
                return original_unlink(path, *args, **kwargs)

            failing.setattr(Path, "unlink", fail_fence)
        assert await runtime.finish_restore() is (fault is None)

    async def facts():
        async with factory() as db:
            generation = await db.scalar(select(User.session_generation).where(User.account_identity == identity))
            epoch = await db.scalar(
                select(Settings.value).where(Settings.key == connector_submissions.STORAGE_EPOCH_KEY)
            )
            session = await db.get(AuthSession, issued.sid)
            return generation, epoch, session.revoked_at

    generation, epoch, revoked = await facts()
    committed = fault in (None, "ambiguous", "unlink")
    assert generation == int(committed)
    assert (epoch != "old-epoch") is committed
    assert (revoked is not None) is committed
    assert fence.exists() is (fault is not None)
    if fault is not None:
        assert runtime.restore_fenced
        assert not runtime.admitting
        # Startup uses the very same finalizer after a crash/unknown commit.
        # Repeating invalidation is safe; an old credential never revives.
        monkeypatch.setattr(database, "async_session", factory)
        await runtime.startup()
        try:
            assert runtime.admitting
            assert not fence.exists()
            recovered_generation, recovered_epoch, recovered_revoked = await facts()
            assert recovered_generation == generation + 1
            assert recovered_epoch != epoch
            assert recovered_revoked is not None
        finally:
            await runtime.shutdown()


async def test_notice_starts_after_asgi_delivery_even_with_operational_hold(monkeypatch):
    # A farm's operational pause does not prevent an administrator signing in.
    guard = type("Guard", (), {"ready": False, "allowed": lambda self: False})()
    guard.mark_ready = lambda: setattr(guard, "ready", True)
    monkeypatch.setattr(farm_module, "farm_startup_guard", guard)
    monkeypatch.setattr(auth_runtime, "admitting", False)
    notice = CredentialRolloutRuntime(retry_seconds=0.01)
    attempted = asyncio.Event()

    async def broken_notice():
        assert guard.ready and auth_runtime.admitting
        attempted.set()
        # Normal _attempt catches render/commit/timeout and leaves a retry.
        return True

    monkeypatch.setattr(notice, "_attempt", broken_notice)
    delivered = asyncio.Event()
    release = asyncio.Event()

    async def application(scope, receive, send):
        notice.start(lambda: guard.ready)
        await send({"type": "lifespan.startup.complete"})

    async def server_send(message):
        delivered.set()
        await release.wait()

    task = asyncio.create_task(farm_module.FarmReadyMiddleware(application)({"type": "lifespan"}, None, server_send))
    try:
        await asyncio.wait_for(delivered.wait(), 2)
        await asyncio.sleep(0.06)
        assert not guard.ready and not auth_runtime.admitting and not attempted.is_set()
        release.set()
        await asyncio.wait_for(task, 2)
        await asyncio.wait_for(attempted.wait(), 2)
        assert not guard.allowed()
        assert auth_runtime.admitting
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert await notice.shutdown(2)


async def test_owned_auth_operation_blocks_restore_without_cancelling_it():
    runtime = AuthRuntime()
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = AsyncMock()

    async def callback():
        async with runtime.operation():
            entered.set()
            await release.wait()
            await completed()

    task = asyncio.create_task(callback())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not await runtime.quiesce_for_restore(0.01)
        assert not task.done()
        assert not runtime.admitting
        release.set()
        await task
        assert await runtime.quiesce_for_restore(1)
        completed.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_real_restore_http_does_not_wait_for_its_own_authentication(async_client, monkeypatch):
    from io import BytesIO
    from zipfile import ZipFile

    from fastapi import HTTPException

    from backend.app.api.routes import settings as settings_routes
    from backend.app.core import db_portable
    from backend.app.services import backup_files, cloud_credentials, library_scan, obico_history_files
    from backend.app.services.connector_runtime import connector_runtime
    from backend.app.services.credential_rollout import credential_rollout
    from backend.app.services.farm_startup_guard import farm_startup_guard
    from backend.app.services.slice_dispatch import slice_dispatch

    class Files:
        committed = False

        def __init__(self, *args):
            pass

        def finish(self):
            pass

    # This test reaches the real authenticated route and its drain ordering,
    # then stops before any live service shutdown or storage replacement.
    monkeypatch.setattr(backup_files, "FileRestore", Files)
    monkeypatch.setattr(db_portable, "validate_sqlite_backup", lambda *a, **kw: None)
    monkeypatch.setattr(backup_files, "validate_staged_queue_spool", lambda *a: None)
    monkeypatch.setattr(obico_history_files, "validate_history", lambda *a: None)
    monkeypatch.setattr(farm_startup_guard, "hold", lambda *a: None)
    monkeypatch.setattr(cloud_credentials, "close_admission", lambda: None)
    for owner in (slice_dispatch, library_scan):
        monkeypatch.setattr(owner, "close_admission", lambda: None)
        monkeypatch.setattr(owner, "quiesce_for_restore", AsyncMock(return_value=True))
    monkeypatch.setattr(credential_rollout, "quiesce_for_restore", AsyncMock(return_value=True))
    monkeypatch.setattr(connector_runtime, "clear_restore_fence", lambda: None)
    monkeypatch.setattr(settings_routes, "_quiesce_part_render", AsyncMock(side_effect=HTTPException(409, "test stop")))
    monkeypatch.setattr(auth_runtime, "admitting", True)
    real_drain = auth_runtime.quiesce_for_restore
    drained = []

    async def observed_drain():
        result = await real_drain(0.1)
        drained.append(result)
        return result

    monkeypatch.setattr(auth_runtime, "quiesce_for_restore", observed_drain)
    body = BytesIO()
    with ZipFile(body, "w") as backup:
        backup.writestr("bamdude.db", b"preflight is outside this test")
    response = await async_client.post(
        "/api/v1/settings/restore", files={"file": ("backup.zip", body.getvalue(), "application/zip")}
    )
    assert drained == [True], response.text
    assert response.status_code == 409 and "test stop" in response.text
    assert not auth_runtime.admitting


@pytest.mark.parametrize("revoke_during_preflight", [False, True])
async def test_restore_http_rechecks_original_actor_before_any_file_move(
    async_client, db_session, monkeypatch, revoke_during_preflight
):
    from io import BytesIO
    from zipfile import ZipFile

    from backend.app.api.routes import settings as settings_routes
    from backend.app.core import db_portable
    from backend.app.mcp_server.runtime import mcp_runtime
    from backend.app.services import backup_files, cloud_credentials, library_scan, obico_history_files
    from backend.app.services.background_dispatch import background_dispatch
    from backend.app.services.connector_runtime import connector_runtime
    from backend.app.services.credential_rollout import credential_rollout
    from backend.app.services.farm_startup_guard import farm_startup_guard
    from backend.app.services.measurement_buffer import measurement_buffer
    from backend.app.services.notification_service import notification_service
    from backend.app.services.obico_detection import obico_detection_service
    from backend.app.services.print_scheduler import scheduler
    from backend.app.services.slice_dispatch import slice_dispatch
    from backend.app.services.smart_plug_manager import smart_plug_manager
    from backend.app.services.virtual_printer import virtual_printer_manager
    from backend.app.services.zigbee.coordinator import zigbee_coordinator
    from backend.app.services.zigbee.poller import zigbee_poller
    from backend.app.services.zigbee.supervisor import zigbee_supervisor

    actor = await db_session.scalar(select(User).where(User.username == "test_admin"))
    original = auth_lifetime.capture_account(actor)
    await db_session.rollback()
    moved = []

    class Files:
        committed = False

        def __init__(self, *args):
            pass

        def prepare(self):
            moved.append(True)
            # Stop exactly at the first destructive boundary. Never replace a
            # real database or run any device/service shutdown in this probe.
            raise RuntimeError("test boundary reached")

        def finish(self):
            pass

    monkeypatch.setattr(backup_files, "FileRestore", Files)
    monkeypatch.setattr(db_portable, "validate_sqlite_backup", lambda *a, **kw: None)
    monkeypatch.setattr(backup_files, "validate_staged_queue_spool", lambda *a: None)
    monkeypatch.setattr(obico_history_files, "validate_history", lambda *a: None)
    real_work = db_portable._file_work

    async def work(function, *args, **kwargs):
        if function is obico_history_files.validate_history and revoke_during_preflight:
            await auth_lifetime.stage_end_sessions(db_session, original)
            await db_session.commit()
        return await real_work(function, *args, **kwargs)

    monkeypatch.setattr(db_portable, "_file_work", work)
    monkeypatch.setattr(farm_startup_guard, "hold", Mock())
    monkeypatch.setattr(farm_module, "notify_change", AsyncMock())
    monkeypatch.setattr(cloud_credentials, "close_admission", Mock())
    for owner in (slice_dispatch, library_scan):
        monkeypatch.setattr(owner, "close_admission", Mock())
        monkeypatch.setattr(owner, "quiesce_for_restore", AsyncMock(return_value=True))
    monkeypatch.setattr(credential_rollout, "quiesce_for_restore", AsyncMock(return_value=True))
    monkeypatch.setattr(settings_routes, "_quiesce_part_render", AsyncMock())
    monkeypatch.setattr(settings_routes, "_quiesce_library_previews", AsyncMock())
    monkeypatch.setattr(mcp_runtime, "quiesce_for_restore", AsyncMock(return_value=True))
    monkeypatch.setattr(connector_runtime, "quiesce_for_restore", AsyncMock(return_value=True))
    monkeypatch.setattr(connector_runtime, "clear_restore_fence", Mock())
    monkeypatch.setattr(obico_detection_service, "quiesce", AsyncMock(return_value=True))
    monkeypatch.setattr(virtual_printer_manager, "configure", AsyncMock())
    monkeypatch.setattr(scheduler, "stop", Mock())
    monkeypatch.setattr(smart_plug_manager, "stop_scheduler", AsyncMock())
    monkeypatch.setattr(measurement_buffer, "retire", Mock())
    monkeypatch.setattr(notification_service, "stop_digest_scheduler", Mock())
    for owner in (background_dispatch, zigbee_supervisor, zigbee_poller, zigbee_coordinator):
        monkeypatch.setattr(owner, "stop", AsyncMock())
    monkeypatch.setattr(auth_runtime, "admitting", True)
    monkeypatch.setattr(auth_runtime, "_restore_drained", False)
    body = BytesIO()
    with ZipFile(body, "w") as backup:
        backup.writestr("bamdude.db", b"preflight is outside this test")
    response = await async_client.post(
        "/api/v1/settings/restore", files={"file": ("backup.zip", body.getvalue(), "application/zip")}
    )
    assert not auth_runtime.admitting
    if revoke_during_preflight:
        assert response.status_code == 401, response.text
        assert moved == []
    else:
        assert response.status_code == 500, response.text
        assert moved == [True]
