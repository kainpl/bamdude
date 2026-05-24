"""Tests for the K-profile auto-link engine (services/kprofile_autolink.py)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.models.filament_calibration import FilamentCalibration
from backend.app.models.printer import Printer
from backend.app.models.spool import Spool
from backend.app.models.spool_k_profile import SpoolKProfile
from backend.app.models.spoolman_k_profile import SpoolmanKProfile
from backend.app.services.kprofile_autolink import (
    autolink_spool,
    autolink_spoolman_spool,
    propagate_calibration_to_spools,
    select_matching_calibrations,
)

_PCOUNT = 0


async def _make_printer(db) -> Printer:
    global _PCOUNT
    _PCOUNT += 1
    p = Printer(
        name=f"P{_PCOUNT}",
        serial_number=f"SN{_PCOUNT:04d}",
        ip_address="10.0.0.1",
        access_code="0000",
        model="X1C",
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


def _cal(printer_id, *, fid="GFG99", ndia=0.4, vol="standard", ext=0, k=0.02, active=False, name="x"):
    return FilamentCalibration(
        printer_id=printer_id,
        filament_id=fid,
        nozzle_diameter=ndia,
        nozzle_volume_type=vol,
        extruder_id=ext,
        pa_k_value=k,
        cali_mode="pa_line",
        source="printer_sync",
        is_active=active,
        name=name,
    )


@pytest.mark.asyncio
async def test_select_matching_picks_active_then_newest(db_session):
    p = await _make_printer(db_session)
    db_session.add_all(
        [
            _cal(p.id, k=0.02, active=False, name="old"),
            _cal(p.id, k=0.03, active=True, name="active"),
            _cal(p.id, ndia=0.6, k=0.04, active=False, name="06"),
        ]
    )
    await db_session.commit()

    rows = await select_matching_calibrations(db=db_session, printer_id=p.id, filament_id="GFG99")
    by_combo = {(r.nozzle_diameter, r.extruder_id): r for r in rows}
    assert by_combo[(0.4, 0)].name == "active"  # active wins over newer-inactive
    assert by_combo[(0.6, 0)].name == "06"
    assert len(rows) == 2  # one per combo


@pytest.mark.asyncio
async def test_autolink_spool_creates_auto_rows_and_activates(db_session):
    p = await _make_printer(db_session)
    fc = _cal(p.id, k=0.03, active=False, name="auto")
    db_session.add(fc)
    spool = Spool(material="PETG", resolved_filament_id="GFG99")
    db_session.add(spool)
    await db_session.commit()

    await autolink_spool(db=db_session, spool=spool)
    await db_session.commit()

    links = (await db_session.execute(select(SpoolKProfile).where(SpoolKProfile.spool_id == spool.id))).scalars().all()
    assert len(links) == 1
    assert links[0].auto_linked is True
    assert links[0].filament_calibration_id == fc.id
    await db_session.refresh(fc)
    assert fc.is_active is True  # auto-activated (no prior active in combo)


@pytest.mark.asyncio
async def test_autolink_spool_respects_manual_link(db_session):
    p = await _make_printer(db_session)
    fc_manual = _cal(p.id, k=0.01, active=True, name="manual")
    fc_auto = _cal(p.id, k=0.03, active=False, name="auto")
    spool = Spool(material="PETG", resolved_filament_id="GFG99")
    db_session.add_all([fc_manual, fc_auto, spool])
    await db_session.commit()
    db_session.add(
        SpoolKProfile(
            spool_id=spool.id,
            printer_id=p.id,
            extruder=0,
            filament_calibration_id=fc_manual.id,
            auto_linked=False,
        )
    )
    await db_session.commit()

    await autolink_spool(db=db_session, spool=spool)
    await db_session.commit()

    links = (await db_session.execute(select(SpoolKProfile).where(SpoolKProfile.spool_id == spool.id))).scalars().all()
    assert len(links) == 1  # manual preserved, no auto row added for same combo
    assert links[0].auto_linked is False


@pytest.mark.asyncio
async def test_autolink_spool_removes_stale_auto_row(db_session):
    """An auto row whose filament no longer matches is removed."""
    p = await _make_printer(db_session)
    fc = _cal(p.id, fid="GFL99", name="pla")
    spool = Spool(material="PETG", resolved_filament_id="GFG99")  # no GFG99 calibration exists
    db_session.add_all([fc, spool])
    await db_session.commit()
    db_session.add(
        SpoolKProfile(
            spool_id=spool.id,
            printer_id=p.id,
            extruder=0,
            filament_calibration_id=fc.id,
            auto_linked=True,
        )
    )
    await db_session.commit()

    await autolink_spool(db=db_session, spool=spool)
    await db_session.commit()

    links = (await db_session.execute(select(SpoolKProfile).where(SpoolKProfile.spool_id == spool.id))).scalars().all()
    assert links == []


@pytest.mark.asyncio
async def test_autolink_spoolman_spool(db_session):
    p = await _make_printer(db_session)
    fc = _cal(p.id, k=0.03, name="auto")
    db_session.add(fc)
    await db_session.commit()

    await autolink_spoolman_spool(db=db_session, spoolman_spool_id=1234, resolved_filament_id="GFG99")
    await db_session.commit()

    links = (
        (await db_session.execute(select(SpoolmanKProfile).where(SpoolmanKProfile.spoolman_spool_id == 1234)))
        .scalars()
        .all()
    )
    assert len(links) == 1
    assert links[0].auto_linked is True
    assert links[0].filament_calibration_id == fc.id


@pytest.mark.asyncio
async def test_propagate_calibration_relinks_matching_spools(db_session):
    p = await _make_printer(db_session)
    fc = _cal(p.id, k=0.03, name="auto")
    spool = Spool(material="PETG", resolved_filament_id="GFG99")
    db_session.add_all([fc, spool])
    await db_session.commit()

    await propagate_calibration_to_spools(db=db_session, printer_id=p.id, filament_ids={"GFG99"})
    await db_session.commit()

    links = (await db_session.execute(select(SpoolKProfile).where(SpoolKProfile.spool_id == spool.id))).scalars().all()
    assert len(links) == 1
    assert links[0].auto_linked is True
