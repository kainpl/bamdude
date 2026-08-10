"""The rest of wave 0: four defects, all "we looked in the wrong place".

* **Calibration nozzles** — ``compute_calibration_supports`` asked ``NozzleInfo``
  for ``diameter`` / ``type`` / ``flow_type``; it carries ``nozzle_diameter`` /
  ``nozzle_type`` / ``nozzle_flow``. All three ``getattr`` defaults fired, so
  every nozzle arrived all-None and the wizard fell back to a hardcoded 0.4 mm.
* **Active extruder** — read from ``state`` bit 8, the low bit of BS's TARGET
  field, where the current extruder is bits 4..7.
* **Chamber sets** — ``.strip().upper()`` leaves the space inside ``"H2D Pro"``,
  which is exactly what our own model map emits.
* **UI-only HMS actions** — publish nothing by design, and the route then waited
  for an acknowledgement that could not come.
"""

from __future__ import annotations

import pytest

from backend.app.services.bambu_mqtt import HMS_UI_ONLY_ACTIONS, NozzleInfo, PrinterState
from backend.app.services.printer_capabilities import compute_calibration_supports
from backend.app.services.printer_manager import (
    supports_airduct,
    supports_chamber_heater,
    supports_chamber_temp,
)


class TestCalibrationReadsTheNozzleFieldsThatExist:
    def test_a_reported_nozzle_is_not_all_none(self) -> None:
        state = PrinterState()
        state.nozzles = [NozzleInfo(nozzle_type="hardened_steel", nozzle_flow="high_flow", nozzle_diameter="0.6")]

        nozzles = compute_calibration_supports(state, "H2D")["nozzles"]

        assert nozzles == [{"id": 0, "diameter": 0.6, "type": "hardened_steel", "flow_type": "high_flow"}]

    def test_diameter_is_a_number_because_the_contract_says_so(self) -> None:
        """``client.ts`` declares ``diameter: number | null`` and the wizard
        compares it — the firmware reports a string."""
        state = PrinterState()
        state.nozzles = [NozzleInfo(nozzle_diameter="0.4")]

        assert compute_calibration_supports(state, "X1C")["nozzles"][0]["diameter"] == 0.4

    def test_an_unreported_nozzle_answers_none_not_zero(self) -> None:
        """None means "printer has not said"; 0.0 would be a diameter."""
        state = PrinterState()
        state.nozzles = [NozzleInfo()]

        n = compute_calibration_supports(state, "X1C")["nozzles"][0]
        assert n["diameter"] is None
        assert n["type"] is None
        assert n["flow_type"] is None

    def test_a_06_farm_no_longer_looks_like_a_04_one(self) -> None:
        """The consequence that made this worth doing: the wizard's fallback is
        ``?? 0.4``, so an all-None nozzle filed every profile under 0.4."""
        state = PrinterState()
        state.nozzles = [NozzleInfo(nozzle_diameter="0.8")]

        assert compute_calibration_supports(state, "X1C")["nozzles"][0]["diameter"] != 0.4


class TestChamberLookupsSurviveTheirOwnModelNames:
    @pytest.mark.parametrize("model", ["H2D Pro", "H2DPRO", "h2d pro"])
    def test_h2d_pro_is_recognised_however_it_is_spelled(self, model: str) -> None:
        """``PRINTER_MODEL_ID_MAP`` emits ``"H2D Pro"`` — with the space — while
        the sets spell it ``H2DPRO``. ``.strip()`` does not touch the middle, so
        preheat never heated this machine's chamber."""
        assert supports_chamber_temp(model) is True
        assert supports_chamber_heater(model) is True
        assert supports_airduct(model) is True

    def test_internal_codes_with_hyphens_still_match(self) -> None:
        """Hyphens are load-bearing in these sets (``BL-P001``), unlike spaces —
        a normaliser that strips both would silently unmatch the X1 family."""
        assert supports_chamber_temp("BL-P001") is True
        assert supports_chamber_temp("BL-P002") is True

    def test_the_heater_answer_comes_from_the_config(self) -> None:
        """``support_chamber_temp_edit`` is exactly this question, and its value
        across the shipped files reproduces the old hardcoded set."""
        for model in ("X1E", "X2D", "H2C", "H2D", "H2D Pro", "H2S"):
            assert supports_chamber_heater(model) is True, model
        for model in ("X1C", "P1S", "P2S", "A1", "A2L"):
            assert supports_chamber_heater(model) is False, model

    def test_a_sensor_only_machine_is_not_a_heater_machine(self) -> None:
        """The three sets stay distinct: P2S has the duct but no heater, X1E the
        heater but no duct."""
        assert supports_chamber_temp("P2S") is True
        assert supports_chamber_heater("P2S") is False
        assert supports_airduct("P2S") is True

        assert supports_chamber_heater("X1E") is True
        assert supports_airduct("X1E") is False


class TestUiOnlyActionsAreDeclared:
    def test_the_set_is_what_the_dispatcher_treats_as_ui_only(self) -> None:
        assert "CHECK_ASSISTANT" in HMS_UI_ONLY_ACTIONS
        assert "REMOVE_CLOSE_BTN" in HMS_UI_ONLY_ACTIONS

    def test_a_real_command_is_not_in_it(self) -> None:
        """Otherwise the route would skip the ack probe for actions that DO
        reach the printer — trading a false failure for a false success."""
        for action in ("RESUME_PRINTING", "STOP_PRINTING", "REFRESH_NOZZLE", "STOP_DRYING"):
            assert action not in HMS_UI_ONLY_ACTIONS
