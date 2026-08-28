"""Local (imported) filament presets are absorbed into the identity mirror
via their own CRUD path, not the cloud loop."""

import json

import pytest
from sqlalchemy import select

from backend.app.models.local_preset import LocalPreset
from backend.app.models.user_filament import UserFilamentPreset
from backend.app.services.filament_preset_sync import (
    absorb_all_local_presets,
    absorb_local_preset,
    drop_local_preset_row,
)


def _lp(name: str, setting: dict, preset_type: str = "filament") -> LocalPreset:
    return LocalPreset(
        name=name,
        preset_type=preset_type,
        source="orcaslicer",
        setting=json.dumps(setting),
        inherits=setting.get("inherits"),
    )


@pytest.mark.asyncio
async def test_absorbs_filament_with_own_filament_id(db_session):
    lp = _lp("My Custom @A1M", {"filament_id": "P4d64437", "inherits": ""})
    db_session.add(lp)
    await db_session.commit()
    await absorb_local_preset(db_session, lp)
    await db_session.commit()
    row = (await db_session.execute(select(UserFilamentPreset))).scalar_one()
    assert row.source == "local" and row.local_preset_id == lp.id
    assert row.family_filament_id == "P4d64437"


@pytest.mark.asyncio
async def test_absorbs_child_via_inherits_walk_and_skips_non_filament(db_session):
    child = _lp("Sunlu @A1M", {"inherits": "Generic PETG @BBL A1M"})
    process = _lp("0.2 Standard", {"inherits": "0.20mm Standard @BBL A1M"}, preset_type="process")
    db_session.add_all([child, process])
    await db_session.commit()
    count = await absorb_all_local_presets(db_session)
    await db_session.commit()
    assert count == 1
    row = (await db_session.execute(select(UserFilamentPreset))).scalar_one()
    assert row.family_filament_id == "GFG99"


@pytest.mark.asyncio
async def test_drop_removes_the_link_row(db_session):
    lp = _lp("Doomed", {"filament_id": "P0ddd000", "inherits": ""})
    db_session.add(lp)
    await db_session.commit()
    await absorb_local_preset(db_session, lp)
    await db_session.commit()
    await drop_local_preset_row(db_session, lp.id)
    await db_session.commit()
    assert (await db_session.execute(select(UserFilamentPreset))).scalars().first() is None
