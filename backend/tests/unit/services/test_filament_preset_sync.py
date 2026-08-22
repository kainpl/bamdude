"""Server-side mirrors of both clouds' private filament presets, driven by
the sanitized live-shape fixtures."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset
from backend.app.services import filament_preset_sync as sync

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "cloud"
BAMBU = json.loads((FIXTURES / "bambu_slicer_settings.json").read_text(encoding="utf-8"))
ORCA = json.loads((FIXTURES / "orca_sync_pull.json").read_text(encoding="utf-8"))


def _bambu_cloud_mock():
    cloud = AsyncMock()
    cloud.is_authenticated = True
    cloud.get_slicer_settings = AsyncMock(return_value=BAMBU)
    cloud.close = AsyncMock()
    return cloud


@pytest.mark.asyncio
async def test_bambu_sync_mirrors_private_rows_preresolved(db_session):
    with patch.object(sync, "_build_bambu_cloud", AsyncMock(return_value=_bambu_cloud_mock())):
        outcome = await sync.sync_bambu_presets_for_user(db_session, None)
    assert outcome.ok and outcome.upserted == 3
    rows = (await db_session.execute(select(UserFilamentPreset))).scalars().all()
    by_cloud = {r.cloud_id: r for r in rows}
    assert by_cloud["PFUS_CHILD_OF_GENERIC"].family_filament_id == "GFG99"
    assert by_cloud["PFUS_CUSTOM_CHILD"].family_filament_id == "P122e532"  # child pre-resolved by the cloud
    # Custom family upserted from the root row:
    fam = (await db_session.execute(select(UserFilamentFamily))).scalar_one()
    assert fam.filament_id == "P122e532" and fam.alias == "test PETG Basic"
    assert fam.origin == "cloud_bambu"


@pytest.mark.asyncio
async def test_bambu_sync_deletes_vanished_rows_but_keeps_families(db_session):
    with patch.object(sync, "_build_bambu_cloud", AsyncMock(return_value=_bambu_cloud_mock())):
        await sync.sync_bambu_presets_for_user(db_session, None)
    shrunk = json.loads(json.dumps(BAMBU))
    shrunk["filament"]["private"] = shrunk["filament"]["private"][:1]  # customs vanish
    cloud = _bambu_cloud_mock()
    cloud.get_slicer_settings = AsyncMock(return_value=shrunk)
    with patch.object(sync, "_build_bambu_cloud", AsyncMock(return_value=cloud)):
        outcome = await sync.sync_bambu_presets_for_user(db_session, None)
    assert outcome.deleted == 2
    remaining = (await db_session.execute(select(UserFilamentPreset))).scalars().all()
    assert [r.cloud_id for r in remaining] == ["PFUS_CHILD_OF_GENERIC"]
    fam = (await db_session.execute(select(UserFilamentFamily))).scalar_one()
    assert fam.orphaned is True  # marked, never deleted


@pytest.mark.asyncio
async def test_orca_sync_resolves_root_child_and_skips_non_filament(db_session):
    svc = AsyncMock()
    svc.list_profiles = AsyncMock(return_value=ORCA)
    svc.close = AsyncMock()
    with patch.object(sync, "build_authenticated_service", AsyncMock(return_value=svc)):
        outcome = await sync.sync_orca_presets_for_user(db_session, None)
    assert outcome.ok and outcome.upserted == 2  # print profile skipped
    rows = (await db_session.execute(select(UserFilamentPreset))).scalars().all()
    by_name = {r.name: r for r in rows}
    assert by_name["TEST PETG Basic @Bambu Lab A1 mini 0.4 nozzle"].family_filament_id == "P08cb51a"
    # Child resolved by walking inherits against the orca catalog:
    assert by_name["A1 Mini Sunlu PETG"].family_filament_id == "GFG99"
    fam_ids = {f.filament_id for f in (await db_session.execute(select(UserFilamentFamily))).scalars()}
    assert fam_ids == {"P08cb51a"}


@pytest.mark.asyncio
async def test_sync_is_idempotent(db_session):
    with patch.object(sync, "_build_bambu_cloud", AsyncMock(return_value=_bambu_cloud_mock())):
        await sync.sync_bambu_presets_for_user(db_session, None)
        outcome = await sync.sync_bambu_presets_for_user(db_session, None)
    assert outcome.deleted == 0
    rows = (await db_session.execute(select(UserFilamentPreset))).scalars().all()
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_upsert_family_is_ecosystem_global(db_session):
    """Same P-hash from another ecosystem = the SAME family (the id is a
    content hash of the name) — no duplicate row, orphaned flag lifted."""
    db_session.add(
        UserFilamentFamily(
            filament_id="P1234567",
            ecosystem="local",
            alias="Poly PETG X",
            vendor="Poly",
            filament_type="PETG",
            origin="authored",
            orphaned=True,
        )
    )
    await db_session.commit()

    added = await sync._upsert_family(
        db_session,
        filament_id="P1234567",
        ecosystem="orca",
        name="Poly PETG X @Bambu Lab P1S",
        vendor="Poly",
        filament_type="PETG",
        origin="local",
    )
    await db_session.commit()

    assert added is False
    rows = (await db_session.execute(select(UserFilamentFamily))).scalars().all()
    assert len(rows) == 1
    assert rows[0].ecosystem == "local" and rows[0].orphaned is False
