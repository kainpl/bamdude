"""One resolver decides which stored K-profile belongs in a Spoolman-filled AMS slot."""

import pytest

from backend.app.models.filament_calibration import FilamentCalibration
from backend.app.models.printer import Printer
from backend.app.models.spoolman_k_profile import SpoolmanKProfile
from backend.app.services.spoolman_kprofile_link import resolve_spoolman_slot_kprofile

SPOOL_ID = 77


async def _printer(db) -> Printer:
    printer = Printer(
        name="X1C", serial_number="00M09A000000001", ip_address="192.168.1.40", access_code="12345678", model="X1C"
    )
    db.add(printer)
    await db.commit()
    await db.refresh(printer)
    return printer


async def _link(db, printer: Printer, *, filament_id: str, nozzle: float, extruder: int) -> FilamentCalibration:
    fc = FilamentCalibration(
        printer_id=printer.id,
        filament_id=filament_id,
        nozzle_diameter=nozzle,
        nozzle_volume_type="standard",
        extruder_id=extruder,
        cali_mode="pa",
        source="printer_sync",
        name=f"{filament_id}@{nozzle}",
    )
    db.add(fc)
    await db.commit()
    await db.refresh(fc)
    db.add(
        SpoolmanKProfile(
            spoolman_spool_id=SPOOL_ID, printer_id=printer.id, extruder=extruder, filament_calibration_id=fc.id
        )
    )
    await db.commit()
    return fc


async def _resolve(db, printer: Printer, *, nozzle: float = 0.4, extruder: int | None = 0):
    return await resolve_spoolman_slot_kprofile(
        db, printer_id=printer.id, spoolman_spool_id=SPOOL_ID, nozzle_diameter=nozzle, slot_extruder=extruder
    )


@pytest.mark.asyncio
async def test_no_link_resolves_to_nothing(db_session):
    printer = await _printer(db_session)
    assert await _resolve(db_session, printer) is None


@pytest.mark.asyncio
async def test_a_link_for_another_nozzle_is_not_this_slots(db_session):
    printer = await _printer(db_session)
    await _link(db_session, printer, filament_id="GFL96", nozzle=0.6, extruder=0)
    assert await _resolve(db_session, printer, nozzle=0.4) is None
    # 0.05 is the tolerance the assign routes use, not an exact comparison.
    assert (await _resolve(db_session, printer, nozzle=0.62)).filament_id == "GFL96"


@pytest.mark.asyncio
async def test_the_slots_own_extruder_wins_over_the_other_one(db_session):
    printer = await _printer(db_session)
    await _link(db_session, printer, filament_id="GFL96", nozzle=0.4, extruder=0)
    await _link(db_session, printer, filament_id="GFB99", nozzle=0.4, extruder=1)
    assert (await _resolve(db_session, printer, extruder=1)).filament_id == "GFB99"
    assert (await _resolve(db_session, printer, extruder=0)).filament_id == "GFL96"


@pytest.mark.asyncio
async def test_an_unknown_extruder_takes_the_first_nozzle_match(db_session):
    """A single-extruder printer reports no map at all — the link still applies."""
    printer = await _printer(db_session)
    await _link(db_session, printer, filament_id="GFL96", nozzle=0.4, extruder=1)
    assert (await _resolve(db_session, printer, extruder=None)).filament_id == "GFL96"
