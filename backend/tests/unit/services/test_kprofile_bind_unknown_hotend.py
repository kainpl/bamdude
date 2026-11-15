"""A slot whose hotend is unknown is not bound as the right-hand nozzle (upstream bff7f6810·5).

On a dual-nozzle printer an AMS the extruder map does not name — it reports
0xE right after connect, or sits behind a Filament Track Switch — was bound by
every binder as extruder 0: the RIGHT hotend's calibration on a slot that may
feed the left. The card already treats that slot as unknown
(``kprofile_lookup``); now the one binder does too. A single-nozzle printer has
one hotend and is untouched.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.app.models.filament_calibration import FilamentCalibration
from backend.app.services.calibration_service import apply_active_calibration_to_slot

GENERIC_PETG = "GFG99"


def _client(extruder_map: dict) -> MagicMock:
    live = MagicMock(slot_id=7, k_value="0.032000", filament_id=GENERIC_PETG, extruder_id=0, nozzle_diameter="0.4")
    live.name = "Generic PETG K0.032"
    client = MagicMock()
    client.state = SimpleNamespace(connected=True, kprofiles=[live], ams_extruder_map=extruder_map, nozzles=[])
    client.extrusion_cali_sel = MagicMock(return_value=True)
    return client


async def _bind(db_session, printer_factory, *, model: str, extruder_map: dict, ams_id: int = 1, slot_id: int = 0):
    printer = await printer_factory(model=model)
    db_session.add(
        FilamentCalibration(
            printer_id=printer.id,
            filament_id=GENERIC_PETG,
            nozzle_diameter=0.4,
            nozzle_volume_type="standard",
            extruder_id=0,
            pa_k_value=0.032,
            cali_mode="pa_line",
            source="printer_sync",
            is_active=True,
            name="Generic PETG K0.032",
        )
    )
    await db_session.commit()
    client = _client(extruder_map)
    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = client
        pm.get_model.return_value = model
        fired, _row = await apply_active_calibration_to_slot(
            db=db_session,
            printer_id=printer.id,
            ams_id=ams_id,
            slot_id=slot_id,
            filament_id=GENERIC_PETG,
            nozzle_diameter=0.4,
            extruder_id=0,
        )
    return fired, client


@pytest.mark.asyncio
async def test_a_dual_nozzle_slot_the_map_does_not_name_is_not_bound(db_session, printer_factory):
    fired, client = await _bind(db_session, printer_factory, model="H2D", extruder_map={"0": 1})
    assert fired is False
    client.extrusion_cali_sel.assert_not_called()


@pytest.mark.asyncio
async def test_a_dual_nozzle_printer_with_no_map_yet_binds_nothing(db_session, printer_factory):
    """Right after connect the map is empty: every AMS is still unknown."""
    fired, client = await _bind(db_session, printer_factory, model="H2D", extruder_map={})
    assert fired is False
    client.extrusion_cali_sel.assert_not_called()


@pytest.mark.asyncio
async def test_a_dual_nozzle_slot_the_map_names_is_bound(db_session, printer_factory):
    fired, client = await _bind(db_session, printer_factory, model="H2D", extruder_map={"1": 0})
    assert fired is True
    client.extrusion_cali_sel.assert_called_once()


@pytest.mark.asyncio
async def test_a_single_nozzle_printer_is_bound_as_before(db_session, printer_factory):
    """No extruder map at all is the ordinary single-nozzle state."""
    fired, client = await _bind(db_session, printer_factory, model="P1S", extruder_map={})
    assert fired is True
    client.extrusion_cali_sel.assert_called_once()


@pytest.mark.asyncio
async def test_the_external_holder_names_its_own_side(db_session, printer_factory):
    """Ext-R / Ext-L say which hotend they feed without the AMS map."""
    fired, client = await _bind(db_session, printer_factory, model="H2D", extruder_map={"0": 1}, ams_id=255, slot_id=1)
    assert fired is True
    client.extrusion_cali_sel.assert_called_once()
