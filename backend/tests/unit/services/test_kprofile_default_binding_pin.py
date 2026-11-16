"""PIN: a spool gets its K-profile by default, through its base filament profile.

The owner's farm rule, measured there and not negotiable: K-profiles belong to
the combination (printer, filament id, nozzle diameter, extruder, flow type),
never to a spool — so a NEW spool of generic PETG, on a nozzle that already
has a generic-PETG calibration, prints with that calibration without anybody
linking anything. These tests walk the real path end to end: the autolink at
spool creation, then the one binder with the slot's live table.

Written before the G07 K-area changes and kept green UNEDITED through them.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.filament_calibration import FilamentCalibration
from backend.app.models.printer import Printer
from backend.app.models.spool import Spool
from backend.app.models.spool_k_profile import SpoolKProfile
from backend.app.services.calibration_service import apply_active_calibration_to_slot
from backend.app.services.kprofile_autolink import autolink_spool

GENERIC_PETG = "GFG99"


async def _printer(db) -> Printer:
    printer = Printer(
        name="Pin P1S",
        serial_number="PINK0001",
        ip_address="10.0.0.9",
        access_code="0000",
        model="P1S",
    )
    db.add(printer)
    await db.commit()
    await db.refresh(printer)
    return printer


def _calibration(printer_id: int, *, active: bool) -> FilamentCalibration:
    return FilamentCalibration(
        printer_id=printer_id,
        filament_id=GENERIC_PETG,
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        pa_k_value=0.032,
        cali_mode="pa_line",
        source="printer_sync",
        is_active=active,
        cali_idx=2,
        name="Generic PETG K0.032",
    )


def _client_with_live_profile(*, live_index: int = 7) -> MagicMock:
    """A connected printer whose 0.4 table holds the calibration at ``live_index``."""
    live = MagicMock(
        slot_id=live_index,
        k_value="0.032000",
        filament_id=GENERIC_PETG,
        extruder_id=0,
        nozzle_diameter="0.4",
    )
    live.name = "Generic PETG K0.032"
    client = MagicMock()
    client.state.connected = True
    client.state.kprofiles = [live]
    client.extrusion_cali_sel = MagicMock(return_value=(True, "0"))
    return client


@pytest.mark.asyncio
async def test_a_new_generic_petg_spool_prints_with_the_nozzles_generic_petg_calibration(db_session):
    """The whole default: create the spool, nothing else — it is linked and bound."""
    printer = await _printer(db_session)
    calibration = _calibration(printer.id, active=False)
    db_session.add(calibration)
    await db_session.commit()

    spool = Spool(material="PETG", filament_family_id=GENERIC_PETG)
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)

    created = await autolink_spool(db=db_session, spool=spool)
    await db_session.commit()

    assert created == 1
    links = (await db_session.execute(select(SpoolKProfile).where(SpoolKProfile.spool_id == spool.id))).scalars().all()
    assert [(ln.printer_id, ln.extruder, ln.filament_calibration_id, ln.auto_linked) for ln in links] == [
        (printer.id, 0, calibration.id, True)
    ]
    await db_session.refresh(calibration)
    assert calibration.is_active is True  # the only calibration of the combo becomes the one that prints

    client = _client_with_live_profile(live_index=7)
    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = client
        fired, row = await apply_active_calibration_to_slot(
            db=db_session,
            printer_id=printer.id,
            ams_id=0,
            slot_id=1,
            filament_id=GENERIC_PETG,
            nozzle_diameter=0.4,
            nozzle_volume_type="standard",
            extruder_id=0,
            spool_id=spool.id,
        )

    assert fired is True
    assert row is not None and row.id == calibration.id
    client.extrusion_cali_sel.assert_called_once()
    sent = client.extrusion_cali_sel.call_args.kwargs
    assert (sent["ams_id"], sent["tray_id"], sent["cali_idx"], sent["filament_id"]) == (0, 1, 7, GENERIC_PETG)


@pytest.mark.asyncio
async def test_a_spool_nobody_linked_still_binds_the_active_calibration_of_its_filament(db_session):
    """No link row at all (a Spoolman spool, a spool created before the calibration):
    the combination's ACTIVE calibration is what the slot gets."""
    printer = await _printer(db_session)
    calibration = _calibration(printer.id, active=True)
    db_session.add(calibration)
    await db_session.commit()

    client = _client_with_live_profile(live_index=4)
    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = client
        fired, row = await apply_active_calibration_to_slot(
            db=db_session,
            printer_id=printer.id,
            ams_id=0,
            slot_id=3,
            filament_id=GENERIC_PETG,
            nozzle_diameter=0.4,
        )

    assert fired is True
    assert row is not None and row.id == calibration.id
    sent = client.extrusion_cali_sel.call_args.kwargs
    assert (sent["tray_id"], sent["cali_idx"], sent["filament_id"]) == (3, 4, GENERIC_PETG)


@pytest.mark.asyncio
async def test_another_filaments_calibration_is_never_the_default(db_session):
    """The key includes the filament id: a PETG spool does not borrow a PLA calibration."""
    printer = await _printer(db_session)
    pla = _calibration(printer.id, active=True)
    pla.filament_id = "GFL99"
    pla.name = "Generic PLA K0.020"
    db_session.add(pla)
    await db_session.commit()

    client = _client_with_live_profile()
    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = client
        fired, row = await apply_active_calibration_to_slot(
            db=db_session,
            printer_id=printer.id,
            ams_id=0,
            slot_id=0,
            filament_id=GENERIC_PETG,
            nozzle_diameter=0.4,
        )

    assert fired is False
    assert row is None
    client.extrusion_cali_sel.assert_not_called()
