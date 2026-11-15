"""An AMS-HT is a unit id 128-135, never "a unit that listed one tray" (upstream cacaaf4da legs 5-6).

A regular AMS that lists one of its trays was drawn as an AMS-HT, and the
runout hint could not tell two AMS on different nozzles apart.
"""

from __future__ import annotations

import math

from backend.app.services import auto_queue_ams
from backend.app.services.bambu_mqtt import FilaSwitchState, PrinterState
from backend.app.services.printer_manager import ams_units_for, nozzle_wiring, resolve_expected_tray


def _state(*units: dict) -> PrinterState:
    state = PrinterState()
    state.connected = True
    state.raw_data = {"ams": list(units)}
    return state


def _tray(tray_id: int, material: str = "PLA") -> dict:
    return {"id": str(tray_id), "tray_type": material, "tray_color": "FF0000FF"}


class TestTheUnitIdDecides:
    def test_a_regular_ams_that_lists_one_tray_is_not_an_ams_ht(self):
        units = ams_units_for(_state({"id": "1", "tray": [_tray(2)]}), 1, None)

        assert [(u.id, u.is_ams_ht) for u in units] == [(1, False)]

    def test_an_ams_ht_is_one_whatever_it_lists(self):
        units = ams_units_for(_state({"id": "128", "tray": [_tray(0)]}, {"id": "129", "tray": []}), 1, None)

        assert [(u.id, u.is_ams_ht) for u in units] == [(128, True), (129, True)]

    def test_the_auto_queue_labels_by_the_unit_id(self):
        status = _state({"id": "1", "tray": [_tray(2)]}, {"id": "128", "tray": [_tray(0, "PA-CF")]})

        loaded = auto_queue_ams.build_loaded_filaments(status)

        assert [(f["ams_id"], f["is_ht"]) for f in loaded] == [(1, False), (128, True)]


class TestTheUnitTemperatureIsAFiniteNumber:
    def test_a_string_becomes_a_number(self):
        assert ams_units_for(_state({"id": "0", "temp": "24.5", "tray": []}), 1, None)[0].temp == 24.5

    def test_what_is_not_a_number_is_no_temperature(self):
        for raw in ("", "n/a", None, True, float("nan"), float("inf")):
            temp = ams_units_for(_state({"id": "0", "temp": raw, "tray": []}), 1, None)[0].temp
            assert temp is None or math.isfinite(temp), raw
            assert temp is None, raw


class TestTheRunoutHintIsNarrowedByTheNozzle:
    LAYOUT = [(0, False), (1, False)]

    def test_two_ams_on_different_nozzles(self):
        """The print paused on the active nozzle, so the slot is on an AMS that feeds it."""
        wiring = {"0": 0, "1": 1}

        assert resolve_expected_tray(2, self.LAYOUT, None, 1, wiring) == 6
        assert resolve_expected_tray(2, self.LAYOUT, None, 0, wiring) == 2

    def test_two_ams_on_the_active_nozzle_stay_undecided(self):
        assert resolve_expected_tray(2, self.LAYOUT, None, 0, {"0": 0, "1": 0}) is None

    def test_the_mapping_still_decides_first(self):
        mapping = [(1 << 8) | 2]

        assert resolve_expected_tray(2, self.LAYOUT, mapping, 0, {"0": 0, "1": 1}) == 6

    def test_the_mapping_and_the_wiring_together(self):
        mapping = [(0 << 8) | 2, (1 << 8) | 2]

        assert resolve_expected_tray(2, self.LAYOUT, mapping, 1, {"0": 0, "1": 1}) == 6

    def test_an_ams_ht_on_the_active_nozzle_is_its_unit(self):
        layout = [(0, False), (1, False), (128, True)]
        wiring = {"0": 0, "1": 0, "128": 1}

        assert resolve_expected_tray(0, layout, None, 1, wiring) == 128

    def test_an_ams_the_wiring_does_not_name_leaves_it_undecided(self):
        assert resolve_expected_tray(2, self.LAYOUT, None, 1, {"0": 0}) is None

    def test_without_wiring_nothing_changes(self):
        assert resolve_expected_tray(2, self.LAYOUT, None) is None
        assert resolve_expected_tray(2, [(1, False)], None) == 6

    def test_a_track_switch_gives_no_wiring(self):
        """Behind a Filament Track Switch an AMS can feed either nozzle."""
        state = PrinterState()
        state.ams_extruder_map = {"0": 0, "1": 1}
        assert nozzle_wiring(state) == {"0": 0, "1": 1}

        state.fila_switch = FilaSwitchState(installed=True)
        assert nozzle_wiring(state) is None
