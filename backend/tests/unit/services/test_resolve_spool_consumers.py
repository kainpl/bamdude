"""derive_effective_filament_id is now a thin async wrapper over the
resolver — the family link outranks RFID outranks legacy strings."""

from types import SimpleNamespace

import pytest

from backend.app.models.user_filament import UserFilamentFamily
from backend.app.services.calibration_service import derive_effective_filament_id


@pytest.mark.asyncio
async def test_family_link_wins_over_everything(db_session):
    db_session.add(
        UserFilamentFamily(filament_id="P122e532", ecosystem="bambu", alias="test PETG Basic", origin="cloud_bambu")
    )
    await db_session.commit()
    spool = SimpleNamespace(filament_family_id="P122e532", bambu_filament_id="GFA00", slicer_filament="GFSG99")
    assert await derive_effective_filament_id(spool=spool, db=db_session) == "P122e532"


@pytest.mark.asyncio
async def test_slot_tray_fallback_still_works(db_session):
    assert await derive_effective_filament_id(spool=None, slot_tray_info_idx="GFA00", db=db_session) == "GFA00"
