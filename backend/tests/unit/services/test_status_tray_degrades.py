"""A tray value the status schema cannot read costs that one field, not the printer's push.

Both status payloads build their AMS trays through one pydantic model (``ams_units_for`` /
``vt_trays_for``). Before that, the WebSocket push copied whatever the printer sent; with the shared
model a single value it cannot coerce raised, and the whole status push of that printer was dropped.
"""

from __future__ import annotations

from unittest.mock import patch

from backend.app.services import printer_manager as pm
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.printer_manager import printer_state_to_dict


def _state(tray: dict, vt: dict | None = None) -> PrinterState:
    state = PrinterState()
    state.connected = True
    state.raw_data = {"ams": [{"id": "0", "tray": [tray]}], "vt_tray": [vt] if vt else []}
    return state


def test_an_unreadable_tray_value_degrades_that_field_only():
    state = _state(
        {
            "id": "0",
            "tray_type": "PLA",
            "tray_color": "FF0000FF",
            "remain": "n/a",
            "nozzle_temp_min": "auto",
            "nozzle_temp_max": 240,
            "cali_idx": "none",
            "state": 11,
        }
    )

    tray = printer_state_to_dict(state, 1, "P1S")["ams"][0]["tray"][0]

    assert (tray["tray_type"], tray["tray_color"], tray["nozzle_temp_max"], tray["state"]) == (
        "PLA",
        "FF0000FF",
        240,
        11,
    )
    assert (tray["remain"], tray["nozzle_temp_min"], tray["cali_idx"]) == (0, None, None)


def test_the_external_holder_degrades_the_same_way():
    state = _state({"id": "0"}, vt={"id": 254, "tray_type": "PETG", "nozzle_temp_min": "auto", "drying_temp": []})

    (tray,) = printer_state_to_dict(state, 1, "P1S")["vt_tray"]

    assert (tray["id"], tray["tray_type"], tray["nozzle_temp_min"]) == (254, "PETG", None)


def test_a_readable_tray_is_untouched():
    state = _state({"id": "0", "tray_type": "PLA", "remain": 62, "nozzle_temp_min": 190, "cali_idx": 3, "k": 0.02})

    tray = printer_state_to_dict(state, 1, "P1S")["ams"][0]["tray"][0]

    assert (tray["remain"], tray["nozzle_temp_min"], tray["cali_idx"], tray["k"]) == (62, 190, 3, 0.02)


def test_the_k_resolver_is_built_once_per_push():
    state = _state({"id": "0", "tray_type": "PLA"}, vt={"id": 254, "tray_type": "PETG"})

    with patch.object(pm, "build_slot_k_resolver", wraps=pm.build_slot_k_resolver) as build:
        printer_state_to_dict(state, 1, "P1S")

    assert build.call_count == 1
