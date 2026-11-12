"""Delayed cloud completions cannot cross an account/provider incarnation."""

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from backend.app.api.routes import cloud, orca_cloud
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.services import cloud_credentials as fences

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def admission():
    fences.open_admission()
    yield
    fences.open_admission()


async def owner(db, name="cloud-owner"):
    user = User(username=name, role="admin", is_active=True, cloud_token="old", orca_cloud_token="old-orca")
    db.add(user)
    await db.commit()
    return user


async def test_rotation_is_provider_specific_and_not_browser_generation(db_session):
    user = await owner(db_session)
    bambu = await fences.snapshot(db_session, user, "bambu")
    orca = await fences.snapshot(db_session, user, "orca")
    old_browser_generation = user.session_generation
    await fences.write(db_session, bambu, {"cloud_token": "new"})
    assert not await fences.current(db_session, bambu)
    assert await fences.current(db_session, orca)
    fresh = await db_session.get(User, user.id, populate_existing=True)
    assert fresh.session_generation == old_browser_generation
    with pytest.raises(HTTPException) as exc:
        await fences.write(db_session, bambu, {"cloud_token": "late-refresh"})
    assert exc.value.status_code == 409
    await db_session.rollback()
    assert (await db_session.get(User, bambu.user_id, populate_existing=True)).cloud_token == "new"


async def test_delete_recreate_same_numeric_id_does_not_accept_old_refresh(db_session):
    user = await owner(db_session)
    old_id = user.id
    captured = await fences.snapshot(db_session, user, "bambu")
    await db_session.execute(delete(User).where(User.id == old_id))
    await db_session.commit()
    replacement = User(id=old_id, username="replacement", role="admin", is_active=True, cloud_token="replacement-token")
    db_session.add(replacement)
    await db_session.commit()
    with pytest.raises(HTTPException):
        await fences.write(db_session, captured, {"cloud_token": "stale"})
    await db_session.rollback()
    user = await db_session.get(User, captured.user_id, populate_existing=True)
    assert (await db_session.get(User, old_id, populate_existing=True)).cloud_token == "replacement-token"


async def test_restore_epoch_and_admission_fence_deny_late_publication(db_session):
    user = await owner(db_session)
    db_session.add(Settings(key="connector_storage_epoch", value="old-epoch"))
    await db_session.commit()
    captured = await fences.snapshot(db_session, user, "bambu")
    row = await db_session.scalar(select(Settings).where(Settings.key == "connector_storage_epoch"))
    row.value = "restored-epoch"
    await db_session.commit()
    assert not await fences.current(db_session, captured)
    with pytest.raises(HTTPException):
        await fences.write(db_session, captured, {"cloud_token": "stale"})
    await db_session.rollback()
    user = await db_session.get(User, captured.user_id, populate_existing=True)
    current = await fences.snapshot(db_session, user, "bambu")
    fences.close_admission()
    assert not await fences.current(db_session, current)
    with pytest.raises(HTTPException):
        await fences.write(db_session, current, {"cloud_token": "closed"})


async def test_missing_owner_never_falls_back_to_global_cloud(db_session):
    db_session.add(Settings(key="bambu_cloud_token", value="global-private"))
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await cloud.build_authenticated_cloud(db_session, None)
    assert exc.value.status_code == 401


async def test_late_orca_device_completion_cannot_replace_new_pairing(db_session):
    user = await owner(db_session)
    await orca_cloud._persist_pending_device(db_session, user, "attempt-one", 5, datetime.now(timezone.utc))
    old = await fences.snapshot(db_session, user, "orca")
    await orca_cloud._persist_pending_device(db_session, user, "attempt-two", 5, datetime.now(timezone.utc))
    with pytest.raises(HTTPException) as exc:
        await orca_cloud._persist_tokens(
            db_session, user, "stale-access", "stale-refresh", None, None, None, captured=old
        )
    assert exc.value.status_code == 409
    await db_session.rollback()
    fresh = await db_session.get(User, old.user_id, populate_existing=True)
    assert fresh.orca_cloud_pending_verifier == "attempt-two"
    assert fresh.orca_cloud_token == "old-orca"


async def test_private_filament_name_cache_never_crosses_accounts(db_session, monkeypatch):
    first = await owner(db_session, "first")
    second = await owner(db_session, "second")

    class Cloud:
        is_authenticated = True

        def __init__(self, captured, name):
            self.credential_snapshot, self.name = captured, name

        async def get_slicer_settings(self):
            return {"filament": {"private": [{"setting_id": "same-setting"}]}}

        async def get_setting_detail(self, setting_id):
            return {"filament_id": "same-filament", "name": self.name}

        async def close(self):
            pass

    async def build(db, user):
        captured = await fences.snapshot(db, user, "bambu")
        await db.commit()
        return Cloud(captured, user.username)

    monkeypatch.setattr(cloud, "build_authenticated_cloud", build)
    monkeypatch.setattr(cloud, "_filament_id_name_cache", {})
    assert await cloud.get_filament_id_map(db_session, first) == {"same-filament": "first"}
    assert await cloud.get_filament_id_map(db_session, second) == {"same-filament": "second"}


async def test_completed_old_fetch_does_not_publish_private_cache(db_session, monkeypatch):
    user = await owner(db_session)

    class Cloud:
        is_authenticated = True

        async def get_slicer_settings(self):
            return {"filament": {"private": [{"setting_id": "private-setting"}]}}

        async def get_setting_detail(self, setting_id):
            await fences.write(db_session, self.credential_snapshot, {"cloud_token": "replacement"})
            return {"filament_id": "private-filament", "name": "Old private name"}

        async def close(self):
            pass

    async def build(db, actor):
        service = Cloud()
        service.credential_snapshot = await fences.snapshot(db, actor, "bambu")
        await db.commit()
        return service

    monkeypatch.setattr(cloud, "build_authenticated_cloud", build)
    monkeypatch.setattr(cloud, "_filament_id_name_cache", {})
    with pytest.raises(HTTPException) as exc:
        await cloud.get_filament_id_map(db_session, user)
    assert exc.value.status_code == 409
    assert cloud._filament_id_name_cache == {}


async def test_replacing_remote_account_clears_only_its_bound_provider_rows(db_session):
    from backend.app.models.user_filament import UserFilamentPreset

    first, second = await owner(db_session, "first"), await owner(db_session, "second")
    first_id, second_id = first.id, second.id
    first_identity, second_identity = first.account_identity, second.account_identity
    db_session.add_all(
        [
            UserFilamentPreset(
                name="first-local",
                source="local",
                ecosystem="bambu",
                pushed_cloud_id="A",
                bambu_pushed_owner_identity=first_identity,
            ),
            UserFilamentPreset(
                name="second-local",
                source="local",
                ecosystem="bambu",
                pushed_cloud_id="B",
                bambu_pushed_owner_identity=second_identity,
            ),
            UserFilamentPreset(name="legacy", source="local", ecosystem="bambu", pushed_cloud_id="unknown"),
            UserFilamentPreset(name="first-private", source="cloud_bambu", ecosystem="bambu", owner_user_id=first_id),
            UserFilamentPreset(name="second-private", source="cloud_bambu", ecosystem="bambu", owner_user_id=second_id),
        ]
    )
    await db_session.commit()
    await cloud.store_token(db_session, "different-remote-account", "new@example.test", "global", first)
    rows = {
        row.name: row
        for row in (
            await db_session.scalars(select(UserFilamentPreset).execution_options(populate_existing=True))
        ).all()
    }
    assert rows["first-local"].pushed_cloud_id is None
    assert rows["second-local"].pushed_cloud_id == "B"
    assert rows["legacy"].pushed_cloud_id == "unknown"  # Unknown binding is never guessed/adopted/cleared.
    assert "first-private" not in rows and "second-private" in rows


async def test_late_private_mirror_sync_cannot_repopulate_replaced_account(db_session, monkeypatch):
    from backend.app.models.user_filament import UserFilamentPreset
    from backend.app.services import filament_preset_sync

    user = await owner(db_session)
    captured = await fences.snapshot(db_session, user, "bambu")

    class Cloud:
        is_authenticated = True
        credential_snapshot = captured

        async def get_slicer_settings(self):
            await fences.write(db_session, captured, {"cloud_token": "new-account"}, replace_binding=True)
            return {"filament": {"private": [{"setting_id": "old-private", "name": "Old private preset"}]}}

        async def close(self):
            pass

    async def build(db, user):
        await db.commit()
        return Cloud()

    monkeypatch.setattr(filament_preset_sync, "_build_bambu_cloud", build)
    with pytest.raises(HTTPException) as exc:
        await filament_preset_sync.sync_bambu_presets_for_user(db_session, user)
    assert exc.value.status_code == 409
    await db_session.rollback()
    assert not (await db_session.scalars(select(UserFilamentPreset))).all()
