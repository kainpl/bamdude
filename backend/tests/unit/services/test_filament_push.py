"""Bambu push of authored families (spec B §5): payload carries the family
id, the returned setting_id lands in pushed_cloud_id, re-push is explicit,
partial failure leaves the family working, sync never duplicates."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.local_preset import LocalPreset
from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset
from backend.app.services.filament_authoring import AuthoringError
from backend.app.services.filament_push import PUSH_CAPABLE, push_family


def _seed_root(db_session, *, fid="Pabc1234", name="Poly PETG B @Bambu Lab P1S 0.4 nozzle"):
    preset = LocalPreset(
        name=name,
        preset_type="filament",
        source="authored",
        setting=json.dumps({"name": name, "filament_id": fid, "filament_type": ["PETG"]}),
    )
    db_session.add(preset)
    db_session.add(
        UserFilamentFamily(
            filament_id=fid,
            ecosystem="local",
            alias="Poly PETG B",
            vendor="Poly",
            filament_type="PETG",
            origin="authored",
        )
    )
    return preset


async def _mirror_for(db_session, preset, fid):
    await db_session.flush()
    row = UserFilamentPreset(
        owner_user_id=None,
        ecosystem="orca",
        source="local",
        local_preset_id=preset.id,
        name=preset.name,
        family_filament_id=fid,
    )
    db_session.add(row)
    await db_session.commit()
    return row


def _cloud_mock(created_id="PFUS_NEW1"):
    cloud = AsyncMock()
    cloud.is_authenticated = True
    cloud.create_setting.return_value = {"setting_id": created_id}
    cloud.update_setting.return_value = {"setting_id": "PFUS_NEW2"}
    return cloud


@pytest.mark.asyncio
async def test_push_creates_and_stores_setting_id(db_session):
    preset = _seed_root(db_session)
    row = await _mirror_for(db_session, preset, "Pabc1234")
    cloud = _cloud_mock()
    with patch("backend.app.services.filament_push._build_bambu_cloud", new=AsyncMock(return_value=cloud)):
        results = await push_family(db_session, filament_id="Pabc1234")
    await db_session.commit()
    assert results[0]["status"] == "pushed" and results[0]["setting_id"] == "PFUS_NEW1"
    # payload carried the family id, base_id empty (custom roots have no base)
    args = cloud.create_setting.call_args
    assert args.args[0] == "filament" and args.args[2] == ""
    assert args.args[3]["filament_id"] == "Pabc1234"
    await db_session.refresh(row)
    assert row.pushed_cloud_id == "PFUS_NEW1" and row.push_dirty is False


@pytest.mark.asyncio
async def test_repush_dirty_updates_and_stores_new_id(db_session):
    preset = _seed_root(db_session)
    row = await _mirror_for(db_session, preset, "Pabc1234")
    row.pushed_cloud_id, row.push_dirty = "PFUS_OLD", True
    await db_session.commit()
    cloud = _cloud_mock()
    with patch("backend.app.services.filament_push._build_bambu_cloud", new=AsyncMock(return_value=cloud)):
        results = await push_family(db_session, filament_id="Pabc1234")
    await db_session.commit()
    assert results[0]["status"] == "updated"
    await db_session.refresh(row)
    # update_setting is delete+recreate on Bambu's side -> NEW id must land
    assert row.pushed_cloud_id == "PFUS_NEW2" and row.push_dirty is False


@pytest.mark.asyncio
async def test_pushed_and_clean_is_up_to_date(db_session):
    preset = _seed_root(db_session)
    row = await _mirror_for(db_session, preset, "Pabc1234")
    row.pushed_cloud_id, row.push_dirty = "PFUS_OK", False
    await db_session.commit()
    cloud = _cloud_mock()
    with patch("backend.app.services.filament_push._build_bambu_cloud", new=AsyncMock(return_value=cloud)):
        results = await push_family(db_session, filament_id="Pabc1234")
    assert results[0]["status"] == "up_to_date"
    cloud.create_setting.assert_not_called()
    cloud.update_setting.assert_not_called()


@pytest.mark.asyncio
async def test_partial_failure_is_reported_not_raised(db_session):
    preset = _seed_root(db_session)
    await _mirror_for(db_session, preset, "Pabc1234")
    cloud = _cloud_mock()
    cloud.create_setting.side_effect = RuntimeError("cloud said no")
    with patch("backend.app.services.filament_push._build_bambu_cloud", new=AsyncMock(return_value=cloud)):
        results = await push_family(db_session, filament_id="Pabc1234")
    assert results[0]["status"] == "error" and "cloud said no" in results[0]["detail"]


@pytest.mark.asyncio
async def test_unknown_ecosystems_stay_capability_gated(db_session):
    # Orca went live 2026-08-24 (own client id + sync:write) — the gate now
    # only refuses ecosystems nobody wired.
    assert PUSH_CAPABLE == {"bambu": True, "orca": True}
    with pytest.raises(AuthoringError):
        await push_family(db_session, filament_id="Pabc1234", ecosystem="prusa")


@pytest.mark.asyncio
async def test_push_blobs_creates_cloud_presets_without_local_state(db_session):
    """Cloud-only creation (Bambu-tab flow): blobs go straight to
    create_setting; nothing local records a pushed_cloud_id — the sync will
    mirror the cloud copies as ordinary cloud presets."""
    from backend.app.services.filament_push import push_blobs

    cloud = _cloud_mock()
    blobs = [
        {"name": "Poly PETG C @Bambu Lab P1S 0.4 nozzle", "filament_id": "Pcccc333", "filament_type": ["PETG"]},
        {"name": "Poly PETG C @Bambu Lab X1 Carbon 0.4 nozzle", "filament_id": "Pcccc333", "filament_type": ["PETG"]},
    ]
    with patch("backend.app.services.filament_push._build_bambu_cloud", new=AsyncMock(return_value=cloud)):
        results = await push_blobs(db_session, blobs=blobs, user=None)
    assert [r["status"] for r in results] == ["pushed", "pushed"]
    assert cloud.create_setting.call_count == 2
    args = cloud.create_setting.call_args_list[0]
    assert args.args[0] == "filament" and args.args[2] == ""
    assert args.args[3]["filament_id"] == "Pcccc333"


@pytest.mark.asyncio
async def test_push_blobs_partial_failure_is_reported(db_session):
    from backend.app.services.filament_push import push_blobs

    cloud = _cloud_mock()
    cloud.create_setting.side_effect = [RuntimeError("nope"), {"setting_id": "PFUS_OK2"}]
    blobs = [{"name": "A @P", "filament_id": "Pdddd444"}, {"name": "B @P", "filament_id": "Pdddd444"}]
    with patch("backend.app.services.filament_push._build_bambu_cloud", new=AsyncMock(return_value=cloud)):
        results = await push_blobs(db_session, blobs=blobs, user=None)
    assert [r["status"] for r in results] == ["error", "pushed"]
