"""The user tier of the filament catalog: mirror rows + custom families +
the spool family link (m149)."""

import pytest
from sqlalchemy import select

from backend.app.models.spool import Spool
from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset


@pytest.mark.asyncio
async def test_models_create_and_roundtrip(db_session):
    fam = UserFilamentFamily(
        filament_id="P122e532",
        ecosystem="bambu",
        alias="test PETG Basic",
        vendor="test",
        filament_type="PETG",
        origin="cloud_bambu",
    )
    row = UserFilamentPreset(
        owner_user_id=None,
        ecosystem="bambu",
        source="cloud_bambu",
        cloud_id="PFUS_TEST01",
        name="test PETG Basic @Bambu Lab A1 mini 0.4 nozzle",
        family_filament_id="P122e532",
        vendor="test",
        filament_type="PETG",
        nozzle_temp_min=220,
        nozzle_temp_max=260,
        updated_time="2026-08-10 22:38:20",
    )
    db_session.add_all([fam, row])
    await db_session.commit()

    got = (await db_session.execute(select(UserFilamentPreset))).scalar_one()
    assert got.family_filament_id == "P122e532"
    assert got.synced_at is not None


@pytest.mark.asyncio
async def test_spool_carries_family_link(db_session):
    spool = Spool(brand="Test", material="PETG", filament_family_id="P122e532")
    db_session.add(spool)
    await db_session.commit()
    assert (await db_session.get(Spool, spool.id)).filament_family_id == "P122e532"
