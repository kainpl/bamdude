"""Idempotent startup backfill of spool.filament_family_id (spec A §6):
resolve what the catalog+mirrors can, take the FE-era legacy column second,
leave honest NULLs, never write garbage."""

import pytest
from sqlalchemy import select

from backend.app.models.spool import Spool
from backend.app.services.filament_family_backfill import backfill_spool_families


def _spool(**kw) -> Spool:
    base = {"brand": "B", "material": "PETG"}
    base.update(kw)
    return Spool(**base)


@pytest.mark.asyncio
async def test_backfill_paths_and_idempotency(db_session):
    db_session.add_all(
        [
            _spool(slicer_filament="GFSG99_00", material=""),  # -> catalog (raw)
            _spool(slicer_filament="PFUS_UNKNOWN_XX", material=""),  # -> NULL
            _spool(filament_family_id="GFB00", material=""),  # -> already linked, untouched
        ]
    )
    await db_session.commit()

    summary = await backfill_spool_families(db_session)
    assert summary == {
        "resolved_raw": 1,
        "unresolved": 1,
        "already_linked": 1,
    }

    spools = (await db_session.execute(select(Spool).order_by(Spool.id))).scalars().all()
    assert spools[0].filament_family_id == "GFG99"
    assert spools[1].filament_family_id is None  # NULL, never garbage
    assert spools[2].filament_family_id == "GFB00"

    # Second run touches nothing new.
    summary2 = await backfill_spool_families(db_session)
    assert summary2["resolved_raw"] == 0
    assert summary2["already_linked"] == 2
