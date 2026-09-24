"""Both status shapers read a slot's K through ONE resolver.

The WebSocket payload (``printer_state_to_dict``) and the REST status
(``_build_printer_status``) describe the same tray, and the frontend merges
them. Both keyed the calibration table on ``cali_idx`` alone, so on a machine
whose table holds that index for two nozzles the card showed whichever entry
was listed last — and once the table is filed per nozzle diameter it holds
every nozzle's entries at once, which makes that collision the normal case.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.app.services.bambu_mqtt import KProfile, NozzleInfo, PrinterState
from backend.app.services.printer_manager import printer_state_to_dict


def _profile(cali_idx: int, k_value: str, nozzle: str, extruder: int) -> KProfile:
    return KProfile(
        slot_id=cali_idx,
        extruder_id=extruder,
        nozzle_id="",
        nozzle_diameter=nozzle,
        filament_id="GFL99",
        name=f"Profile {cali_idx}",
        k_value=k_value,
    )


def _dual_nozzle_state() -> PrinterState:
    """An H2D: AMS 0 feeds the left hotend (extruder 1), AMS 1 the right (0).
    Index 3 exists on both hotends with different K, and no tray carries ``k``."""
    state = PrinterState()
    state.connected = True
    state.nozzles = [NozzleInfo(nozzle_diameter="0.4"), NozzleInfo(nozzle_diameter="0.4")]
    state.ams_extruder_map = {"0": 1, "1": 0}
    state.kprofiles = [_profile(3, "0.020000", "0.4", extruder=0), _profile(3, "0.018000", "0.4", extruder=1)]
    state.raw_data = {
        "ams": [
            {"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "cali_idx": 3}]},
            {"id": 1, "tray": [{"id": 0, "tray_type": "PLA", "cali_idx": 3}]},
        ],
        "ams_extruder_map": {"0": 1, "1": 0},
        "vt_tray": [
            {"id": 254, "tray_type": "PLA", "cali_idx": 3},
            {"id": 255, "tray_type": "PLA", "cali_idx": 3},
        ],
    }
    return state


def test_the_websocket_payload_reads_each_slot_on_its_own_hotend():
    payload = printer_state_to_dict(_dual_nozzle_state(), printer_id=1, model="H2D")

    ams_k = {unit["id"]: unit["tray"][0]["k"] for unit in payload["ams"]}
    assert ams_k[0] == pytest.approx(0.018)
    assert ams_k[1] == pytest.approx(0.020)
    vt_k = {tray["id"]: tray["k"] for tray in payload["vt_tray"]}
    assert vt_k[254] == pytest.approx(0.018)  # Ext-L → left hotend
    assert vt_k[255] == pytest.approx(0.020)  # Ext-R → right hotend


def test_a_tray_that_carries_its_own_k_keeps_it():
    state = _dual_nozzle_state()
    state.raw_data["ams"][0]["tray"][0]["k"] = 0.031

    payload = printer_state_to_dict(state, printer_id=1, model="H2D")

    assert payload["ams"][0]["tray"][0]["k"] == pytest.approx(0.031)


@pytest.mark.asyncio
async def test_the_rest_status_reads_each_slot_on_its_own_hotend(async_client, printer_factory):
    printer = await printer_factory(model="H2D")
    with patch("backend.app.api.routes.printers.printer_manager") as pm:
        pm.get_status.return_value = _dual_nozzle_state()
        pm.is_awaiting_plate_clear.return_value = False
        pm.get_drying_targets.return_value = {}
        response = await async_client.get(f"/api/v1/printers/{printer.id}/status")

    assert response.status_code == 200, response.text
    body = response.json()
    ams_k = {unit["id"]: unit["tray"][0]["k"] for unit in body["ams"]}
    assert ams_k[0] == pytest.approx(0.018)
    assert ams_k[1] == pytest.approx(0.020)
    vt_k = {tray["id"]: tray["k"] for tray in body["vt_tray"]}
    assert vt_k[254] == pytest.approx(0.018)
    assert vt_k[255] == pytest.approx(0.020)
