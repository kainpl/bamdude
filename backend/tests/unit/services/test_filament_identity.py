"""The identity resolver: anything -> family. System catalog + mirrors +
legacy fallback; the custom-family regression case is the whole point of the
cycle (a P-hash must resolve to ITS family, never flattened onto generic)."""

from types import SimpleNamespace

import pytest

from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset
from backend.app.services import filament_identity as fi


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw, expected_family, expected_origin",
    [
        ("GFG99", "GFG99", "system"),  # bare filament_id
        ("GFSG99", "GFG99", "system"),  # base setting_id
        ("GFSG99_00", "GFG99", "system"),  # versioned setting_id
        ("PETG", "GFG99", "legacy"),  # bare material name -> generic family
        ("total garbage", None, "unknown"),
    ],
)
async def test_resolve_raw_over_the_legacy_union(db_session, raw, expected_family, expected_origin):
    resolved = await fi.resolve_raw(db_session, raw)
    got = resolved.family.filament_id if resolved.family else None
    assert got == expected_family
    assert resolved.origin == expected_origin


@pytest.mark.asyncio
async def test_resolve_raw_finds_cloud_mirror_by_cloud_id(db_session):
    db_session.add(
        UserFilamentPreset(
            owner_user_id=None,
            ecosystem="bambu",
            source="cloud_bambu",
            cloud_id="PFUS_CUSTOM_ROOT",
            name="test PETG Basic @Bambu Lab A1 mini 0.4 nozzle",
            family_filament_id="P122e532",
            vendor="test",
            filament_type="PETG",
            nozzle_temp_min=220,
            nozzle_temp_max=260,
        )
    )
    db_session.add(
        UserFilamentFamily(
            filament_id="P122e532",
            ecosystem="bambu",
            alias="test PETG Basic",
            vendor="test",
            filament_type="PETG",
            origin="cloud_bambu",
        )
    )
    await db_session.commit()

    resolved = await fi.resolve_raw(db_session, "PFUS_CUSTOM_ROOT")
    assert resolved.family.filament_id == "P122e532"
    assert resolved.origin == "cloud_bambu"
    assert resolved.setting_id == "PFUS_CUSTOM_ROOT"


@pytest.mark.asyncio
async def test_resolve_tray_custom_family_regression(db_session):
    """The flattening bug: a P-hash tray must resolve to ITS family, not generic."""
    db_session.add(
        UserFilamentFamily(
            filament_id="P122e532",
            ecosystem="bambu",
            alias="test PETG Basic",
            vendor="test",
            filament_type="PETG",
            origin="cloud_bambu",
        )
    )
    await db_session.commit()
    resolved = await fi.resolve_tray(db_session, "P122e532")
    assert resolved.family.filament_id == "P122e532"
    assert resolved.display_name == "test PETG Basic"


@pytest.mark.asyncio
async def test_resolve_tray_system_and_unknown(db_session):
    assert (await fi.resolve_tray(db_session, "GFA00")).display_name == "Bambu PLA Basic"
    unknown = await fi.resolve_tray(db_session, "P0000000")
    assert unknown.family is None and unknown.origin == "unknown"


@pytest.mark.asyncio
async def test_resolve_spool_precedence(db_session):
    # 1) explicit family link wins
    spool = SimpleNamespace(filament_family_id="GFA00", bambu_filament_id="GFG99", slicer_filament="GFSB00")
    assert (await fi.resolve_spool(db_session, spool)).family.filament_id == "GFA00"
    # 2) RFID id next
    spool = SimpleNamespace(filament_family_id=None, bambu_filament_id="GFG99", slicer_filament="GFSB00")
    assert (await fi.resolve_spool(db_session, spool)).family.filament_id == "GFG99"
    # 3) legacy string last
    spool = SimpleNamespace(filament_family_id=None, bambu_filament_id=None, slicer_filament="GFSB00")
    assert (await fi.resolve_spool(db_session, spool)).family.filament_id == "GFB00"
