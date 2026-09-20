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
        "repaired": 0,
        "cleared": 0,
    }

    spools = (await db_session.execute(select(Spool).order_by(Spool.id))).scalars().all()
    assert spools[0].filament_family_id == "GFG99"
    assert spools[1].filament_family_id is None  # NULL, never garbage
    assert spools[2].filament_family_id == "GFB00"

    # Second run touches nothing new.
    summary2 = await backfill_spool_families(db_session)
    assert summary2["resolved_raw"] == 0
    assert summary2["already_linked"] == 2


@pytest.mark.asyncio
async def test_backfill_repairs_a_link_the_catalog_cannot_resolve(db_session):
    """A non-NULL link is not proof of a good link. m079 stripped the S off
    every ``GFS*`` code, which turned the support families (``GFS00`` Support
    W …) into ``GF00`` — an id that exists nowhere — and m150 copied that raw
    into ``filament_family_id``. "Only NULLs are filled" then protected the
    garbage forever, and every edit of such a spool was refused with
    ``unknown filament family`` (2026-09-04). A link the resolver rejects is
    re-derived from the slicer code; when nothing can re-derive it, the link
    is cleared — an honest NULL the picker can fill, instead of a value the
    server will refuse."""
    db_session.add_all(
        [
            _spool(slicer_filament="GFS00", filament_family_id="GF00", material=""),  # -> repaired to GFS00
            _spool(slicer_filament=None, filament_family_id="GFZZ99", material=""),  # -> cleared
            _spool(slicer_filament="GFA00", filament_family_id="GFA00", material=""),  # -> untouched
        ]
    )
    await db_session.commit()

    summary = await backfill_spool_families(db_session)
    assert summary == {
        "resolved_raw": 0,
        "unresolved": 0,
        "already_linked": 1,
        "repaired": 1,
        "cleared": 1,
    }

    spools = (await db_session.execute(select(Spool).order_by(Spool.id))).scalars().all()
    assert spools[0].filament_family_id == "GFS00"
    assert spools[1].filament_family_id is None
    assert spools[2].filament_family_id == "GFA00"

    # Second run: the repaired link is now a good link, the cleared one is an
    # honest NULL that still has nothing to resolve from.
    summary2 = await backfill_spool_families(db_session)
    assert summary2 == {
        "resolved_raw": 0,
        "unresolved": 1,
        "already_linked": 2,
        "repaired": 0,
        "cleared": 0,
    }
