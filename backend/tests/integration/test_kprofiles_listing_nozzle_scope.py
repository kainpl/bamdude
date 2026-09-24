"""The Profiles listing ties each live profile to the cache row of ITS nozzle.

``GET /printers/{id}/kprofiles/?nozzle_diameter=…`` answers with one nozzle's
table and maps each entry to a ``filament_calibration`` id by identity (name +
filament + K). The cache keeps every nozzle's calibrations — the sync no longer
prunes a diameter it holds no table for — so a "PLA Basic, K 0.020" calibrated
on both 0.4 and 0.6 mm is two rows, and the identity alone picked whichever came
first. Notes and spool links then attached to the other nozzle's profile.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.filament_calibration import FilamentCalibration
from backend.app.services.bambu_mqtt import KProfile


def _live(cali_idx: int, nozzle: str) -> KProfile:
    return KProfile(
        slot_id=cali_idx,
        extruder_id=0,
        nozzle_id=f"HS00-{nozzle}",
        nozzle_diameter=nozzle,
        filament_id="GFA00",
        name="PLA Basic",
        k_value="0.020000",
        setting_id="GFSA00",
    )


def _cached(printer_id: int, nozzle: float, cali_idx: int) -> FilamentCalibration:
    return FilamentCalibration(
        printer_id=printer_id,
        filament_id="GFA00",
        filament_setting_id="GFSA00",
        nozzle_diameter=nozzle,
        nozzle_volume_type="standard",
        extruder_id=0,
        pa_k_value=0.02,
        cali_mode="pa_line",
        source="printer_sync",
        is_active=False,
        cali_idx=cali_idx,
        name="PLA Basic",
        nozzle_id=f"HS00-{nozzle}",
    )


@pytest.mark.asyncio
async def test_a_profile_maps_to_the_row_of_its_own_nozzle(async_client, db_session, printer_factory):
    printer = await printer_factory(model="X1C")
    row_04 = _cached(printer.id, 0.4, 3)
    row_06 = _cached(printer.id, 0.6, 5)
    db_session.add_all([row_04, row_06])
    await db_session.commit()

    client = MagicMock()
    client.state.connected = True
    client.state.kprofiles = [_live(3, "0.4"), _live(5, "0.6")]
    client.get_kprofiles = AsyncMock(return_value=[_live(5, "0.6")])
    manager = MagicMock()
    manager.get_client.return_value = client
    manager.ensure_fresh_connection_for_printer = AsyncMock(return_value=True)

    with (
        patch("backend.app.api.routes.kprofiles.printer_manager", manager),
        patch("backend.app.services.calibration_service.printer_manager", manager),
    ):
        response = await async_client.get(f"/api/v1/printers/{printer.id}/kprofiles/?nozzle_diameter=0.6")

    assert response.status_code == 200, response.text
    assert response.json()["fc_id_by_cali_idx"] == {"5": row_06.id}


async def _note_by_setting_id(async_client, printer_id: int, live: list[KProfile]):
    client = MagicMock()
    client.state.connected = True
    client.state.kprofiles = live
    with patch("backend.app.services.printer_manager.printer_manager.get_client", return_value=client):
        return await async_client.put(
            f"/api/v1/printers/{printer_id}/kprofiles/notes", json={"setting_id": "GFSA00", "note": "dry box"}
        )


@pytest.mark.asyncio
async def test_a_note_by_setting_id_lands_on_the_row_of_the_profiles_nozzle(async_client, db_session, printer_factory):
    """The legacy ``setting_id`` hint used to query the cache by identity alone,
    which raised on two rows sharing name + K (a 500) instead of choosing."""
    from sqlalchemy import select

    from backend.app.models.kprofile_note import KProfileNote

    printer = await printer_factory(model="X1C")
    row_04 = _cached(printer.id, 0.4, 3)
    row_06 = _cached(printer.id, 0.6, 5)
    db_session.add_all([row_04, row_06])
    await db_session.commit()

    response = await _note_by_setting_id(async_client, printer.id, [_live(5, "0.6")])

    assert response.status_code == 200, response.text
    notes = (await db_session.execute(select(KProfileNote))).scalars().all()
    assert [n.filament_calibration_id for n in notes] == [row_06.id]


@pytest.mark.asyncio
async def test_the_audit_link_follows_the_edited_profiles_nozzle(db_session, printer_factory):
    """``cali_idx`` 3 is a row on each nozzle; the audit of a 0.6 delete must
    point at the 0.6 calibration, not whichever row the index finds first."""
    from backend.app.api.routes.kprofiles import _resolve_fc_id_for_audit
    from backend.app.schemas.kprofile import KProfileDelete

    printer = await printer_factory(model="X1C")
    row_04 = _cached(printer.id, 0.4, 3)
    row_06 = _cached(printer.id, 0.6, 3)
    db_session.add_all([row_04, row_06])
    await db_session.commit()

    fc_id = await _resolve_fc_id_for_audit(
        db_session,
        printer_id=printer.id,
        profile=KProfileDelete(slot_id=3, nozzle_id="HS00-0.6", nozzle_diameter="0.6", filament_id="GFA00"),
    )

    assert fc_id == row_06.id


@pytest.mark.asyncio
async def test_a_setting_id_two_nozzles_share_names_no_single_row(async_client, db_session, printer_factory):
    """Both tables carry the preset: the hint cannot say which one was meant."""
    printer = await printer_factory(model="X1C")
    db_session.add_all([_cached(printer.id, 0.4, 3), _cached(printer.id, 0.6, 5)])
    await db_session.commit()

    response = await _note_by_setting_id(async_client, printer.id, [_live(3, "0.4"), _live(5, "0.6")])

    assert response.status_code == 404, response.text
