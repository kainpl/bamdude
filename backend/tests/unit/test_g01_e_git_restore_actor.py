"""Original immutable browser authority survives Git I/O only while still valid."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.core.auth_runtime import auth_runtime
from backend.app.models.auth_session import AuthSession
from backend.app.models.git_backup import GitBackupConfig
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.schemas.git_backup import RestoreCategory
from backend.app.services import git_restore
from backend.app.services.git_restore import SETTINGS_PATH, GitRestoreService, _CategoryTally
from backend.tests.auth_helpers import mint_test_session


@pytest.fixture
async def actor_restore(test_engine, monkeypatch):
    auth_runtime.open_admission()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    sessions = []

    def tracked_factory():
        db = factory()
        sessions.append(db)
        return db

    monkeypatch.setattr(git_restore, "async_session", tracked_factory)
    async with factory() as db:
        user = User(username="restore_actor", role="admin", groups=[], is_active=True)
        db.add(user)
        await db.commit()
        issued = await mint_test_session(db, user=user)
        config = GitBackupConfig(repository_url="https://github.com/test/private", access_token="test-only")
        db.add(config)
        await db.commit()
        config_id = config.id
    yield factory, sessions, issued.capture, config_id
    auth_runtime.open_admission()


@pytest.mark.asyncio
async def test_current_actor_success_preserves_original_session_and_canonical_settings(actor_restore):
    factory, sessions, capture, cid = actor_restore
    service = GitRestoreService()

    async def resolve(*args):
        assert not sessions[-1].in_transaction()
        return "a" * 40, "", None

    service._resolve_ref = resolve
    service._read_categories = AsyncMock(return_value=({SETTINGS_PATH: {"settings": {"currency": "EUR"}}}, ""))
    result = await service.run_restore(
        cid, "HEAD", [RestoreCategory.SETTINGS], authority=capture, required_permissions=("settings:update",)
    )
    assert result["success"] and result["results"]["settings"]["restored"] == 1
    async with factory() as db:
        assert await db.scalar(select(Settings.value).where(Settings.key == "currency")) == "EUR"
        assert (await db.get(AuthSession, capture.sid)).revoked_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["revoke", "deactivate", "demote", "recreate"])
@pytest.mark.parametrize("category", [RestoreCategory.SETTINGS, RestoreCategory.KPROFILES])
async def test_remote_payload_cannot_publish_for_stale_original_actor(actor_restore, mutation, category, monkeypatch):
    from backend.app.services.farm_startup_guard import farm_startup_guard

    factory, sessions, capture, cid = actor_restore
    hold = MagicMock()
    monkeypatch.setattr(farm_startup_guard, "hold", hold)
    service = GitRestoreService()
    service._resolve_ref = AsyncMock(return_value=("a" * 40, "", None))

    async def download(*args):
        assert not sessions[-1].in_transaction()
        async with factory() as db:
            user = await db.get(User, capture.user_id)
            if mutation == "revoke":
                (await db.get(AuthSession, capture.sid)).revoked_at = git_restore.datetime.now(
                    git_restore.timezone.utc
                ).replace(tzinfo=None)
            elif mutation == "deactivate":
                user.is_active = False
            elif mutation == "demote":
                user.role = "user"
            else:
                await db.delete(user)
                await db.flush()
                db.add(User(id=capture.user_id, username="restore_actor", role="admin", groups=[]))
            await db.commit()
        return {SETTINGS_PATH: {"settings": {"currency": "EUR"}}}, ""

    service._read_categories = download
    with pytest.raises(HTTPException) as refused:
        await service.run_restore(cid, "HEAD", [category], authority=capture, required_permissions=("settings:update",))
    assert refused.value.status_code in {401, 403}
    hold.assert_not_called()
    async with factory() as db:
        assert await db.scalar(select(Settings).where(Settings.key == "currency")) is None


@pytest.mark.asyncio
async def test_every_committed_category_rechecks_actor(actor_restore):
    factory, sessions, capture, cid = actor_restore
    service = GitRestoreService()
    service._resolve_ref = AsyncMock(return_value=("a" * 40, "", None))
    service._read_categories = AsyncMock(return_value=({}, ""))

    async def archives(db, *args):
        # Simulate the permission generation changing between committed
        # categories, without inventing a username/current-session fallback.
        (await db.get(User, capture.user_id)).session_generation += 1

    service._restore_archives = archives
    service._restore_spools = AsyncMock()
    with pytest.raises(HTTPException) as refused:
        await service.run_restore(cid, "HEAD", [RestoreCategory.ARCHIVES, RestoreCategory.SPOOLS], authority=capture)
    assert refused.value.status_code == 401
    service._restore_spools.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_owns_delayed_remote_read_until_actual_restore_completion(actor_restore):
    _, _, capture, cid = actor_restore
    service = GitRestoreService()
    entered, release = asyncio.Event(), asyncio.Event()
    service._resolve_ref = AsyncMock(return_value=("a" * 40, "", None))

    async def download(*args):
        entered.set()
        await release.wait()
        return {SETTINGS_PATH: {"settings": {"currency": "EUR"}}}, ""

    service._read_categories = download
    task = asyncio.create_task(service.run_restore(cid, "HEAD", [RestoreCategory.SETTINGS], authority=capture))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert not await auth_runtime.quiesce_for_restore(0.01)
        release.set()
        with pytest.raises(HTTPException) as refused:
            await asyncio.wait_for(task, 3)
        assert refused.value.status_code == 503
        assert await auth_runtime.quiesce_for_restore(1)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_kprofile_final_guard_after_remote_lookup_prevents_revoked_send(
    actor_restore, printer_factory, monkeypatch
):
    factory, _, capture, _ = actor_restore
    await printer_factory(serial_number="RESTORE-K")
    client = MagicMock()
    client.state.connected = True

    async def lookup(**kwargs):
        async with factory() as db:
            (await db.get(User, capture.user_id)).is_active = False
            await db.commit()
        return []

    client.get_kprofiles = lookup
    monkeypatch.setattr(git_restore.printer_manager, "get_client", lambda pid: client)
    payload = {"kprofiles/RESTORE-K/0.4.json": {"profiles": [{"filament_id": "PLA", "name": "PLA"}]}}
    async with factory() as db:
        with pytest.raises(HTTPException):
            await GitRestoreService()._restore_kprofiles(db, payload, _CategoryTally(), authority=capture)
    client.set_kprofiles_batch.assert_not_called()


@pytest.mark.asyncio
async def test_kprofile_each_delayed_batch_rechecks_and_has_no_checkout_during_io(
    actor_restore, printer_factory, monkeypatch
):
    factory, _, capture, _ = actor_restore
    await printer_factory(serial_number="RESTORE-K")
    client = MagicMock()
    client.state.connected = True
    client.get_kprofiles = AsyncMock(return_value=[])
    client.set_kprofiles_batch.return_value = "1"
    async with factory() as db:

        async def lookup(**kwargs):
            assert not db.in_transaction()
            return []

        client.get_kprofiles = lookup

        async def ack(seq):
            assert not db.in_transaction()
            async with factory() as mutation:
                (await mutation.get(AuthSession, capture.sid)).revoked_at = git_restore.datetime.now(
                    git_restore.timezone.utc
                ).replace(tzinfo=None)
                await mutation.commit()
            return True, ""

        client.await_cali_ack = ack
        monkeypatch.setattr(git_restore.printer_manager, "get_client", lambda pid: client)
        payload = {
            f"kprofiles/RESTORE-K/{nozzle}.json": {"profiles": [{"filament_id": "PLA", "name": "PLA"}]}
            for nozzle in ["0.4", "0.6"]
        }
        tally = _CategoryTally()
        with pytest.raises(HTTPException):
            await GitRestoreService()._restore_kprofiles(db, payload, tally, authority=capture)
    client.set_kprofiles_batch.assert_called_once()
    assert tally.restored == 1
