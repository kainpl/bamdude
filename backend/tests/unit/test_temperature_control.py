"""Setting a temperature is two commands per heater, not one — and which one is
not ours to choose.

Registry N6. We sent ``M140`` for the bed unconditionally and had no nozzle
setter at all. BambuStudio branches both:

* ``command_set_bed`` — JSON ``set_bed_temp`` when ``m_support_mqtt_bet_ctrl``
  (``fun`` bit 39, the typo is theirs), ``M140`` when not;
* ``command_set_nozzle`` / ``command_set_nozzle_new`` — ``M104`` only while the
  machine has ONE extruder, and ``set_nozzle_temp`` with an explicit
  ``extruder_index`` as soon as there are two. ⚠️ The split is by nozzle count,
  not by the machine's age: the deputy nozzle has no ``M104`` form at all,
  because the g-code cannot say which one it means.

⚠️ Clamping cuts the ceiling only, and never touches 0. Both are BS's own
behaviour and both matter: ``on_set_bed_temp`` clamps the maximum and says
nothing about the minimum, and ``AddTemp(0)`` lifts "off" out of every bound. A
clamp that raised low values would turn "stop heating" into "heat to 20".
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _client(model: str = "P1S") -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="1.2.3.4", serial_number="P1S0001", access_code="12345678", model=model)
    c._client = MagicMock()
    c.state.connected = True
    return c


def _published(c: BambuMQTTClient) -> dict:
    return json.loads(c._client.publish.call_args[0][1])["print"]


def _gcode(c: BambuMQTTClient) -> str:
    return json.loads(c._client.publish.call_args[0][1])["print"]["param"]


class TestTheBedPicksItsCommand:
    def test_without_the_bit_it_is_gcode(self) -> None:
        c = _client()
        assert c.set_bed_temperature(60) is True

        assert _gcode(c).strip() == "M140 S60"

    def test_with_the_bit_it_is_json(self) -> None:
        """``fun`` bit 39. A machine that offers the JSON command was still
        being driven by g-code."""
        c = _client()
        c.state.print_option_support["mqtt_bed_ctrl"] = True
        assert c.set_bed_temperature(60) is True

        p = _published(c)
        assert p["command"] == "set_bed_temp"
        assert p["temp"] == 60
        assert "sequence_id" in p


class TestTheNozzlePicksItsCommand:
    def test_one_extruder_takes_the_legacy_gcode(self) -> None:
        c = _client()
        assert c.set_nozzle_temperature(220) is True

        assert _gcode(c).strip() == "M104 S220"

    def test_two_extruders_take_the_json_command(self) -> None:
        c = _client(model="H2D")
        c._is_dual_nozzle = True
        assert c.set_nozzle_temperature(220) is True

        p = _published(c)
        assert p["command"] == "set_nozzle_temp"
        assert p["extruder_index"] == 0
        assert p["target_temp"] == 220

    def test_the_deputy_never_takes_gcode(self) -> None:
        """⚠️ There is no ``M104`` that can name the second nozzle, so the JSON
        form is the only one for it whatever else is true."""
        c = _client(model="H2D")
        c._is_dual_nozzle = True
        c.set_nozzle_temperature(220, extruder_index=1)

        assert _published(c)["extruder_index"] == 1

    def test_a_dual_model_that_has_not_reported_yet_still_uses_json(self) -> None:
        """⚠️ Our dual-nozzle flag starts False and turns true only once
        ``device.extruder.info`` has arrived. BS reads a live count; we keep the
        model as a second opinion so an early command does not fall into the
        g-code path, where "which nozzle" cannot be said."""
        c = _client(model="H2D")
        assert c._is_dual_nozzle is False
        c.set_nozzle_temperature(220)

        assert _published(c)["command"] == "set_nozzle_temp"


class TestClamping:
    def test_the_nozzle_is_cut_to_its_ceiling(self) -> None:
        c = _client()
        c.set_nozzle_temperature(9000)

        assert _gcode(c).strip() == "M104 S300"

    def test_a_reported_range_moves_the_ceiling(self) -> None:
        c = _client()
        c.state.nozzle_temp_range = [170, 320]
        c.set_nozzle_temperature(9000)

        assert _gcode(c).strip() == "M104 S320"

    def test_the_bed_is_cut_to_its_ceiling(self) -> None:
        c = _client()
        c.set_bed_temperature(9000)

        assert _gcode(c).strip() == "M140 S120"

    def test_the_mains_voltage_lowers_what_the_bed_accepts(self) -> None:
        """⚠️ 220 V gives a LOWER ceiling on the X1 and O series — the one rule
        here that reads backwards."""
        c = _client(model="X1C")
        c.state.is_220v = True
        c.set_bed_temperature(9000)

        assert _gcode(c).strip() == "M140 S110"

    @pytest.mark.parametrize("setter", ["set_bed_temperature", "set_nozzle_temperature"])
    def test_off_survives_the_clamp(self, setter: str) -> None:
        """A floor applied here would make "stop heating" unreachable."""
        c = _client()
        getattr(c, setter)(0)

        assert _gcode(c).strip().endswith("S0")


class TestTheChamberIsClampedToo:
    @pytest.mark.parametrize(("model", "ceiling"), [("X2D", 65), ("H2D", 65), ("X1E", 60)])
    def test_it_is_cut_to_the_models_own_range(self, model: str, ceiling: int) -> None:
        """⚠️ The ceiling really is per-model — the X1E stops at 60 where the H2
        family goes to 65 — which is why this comes from the mirrored config and
        not from one shared constant."""
        c = _client(model=model)
        assert c.set_chamber_temperature(9000) is True

        assert _published(c)["ctt_val"] == ceiling

    def test_the_locally_tracked_target_is_the_clamped_one(self) -> None:
        """The card reads this back while the printer's own echo is filtered, so
        a pre-clamp value here would show a setpoint that was never sent."""
        c = _client(model="X2D")
        c.set_chamber_temperature(9000)

        assert c.state.temperatures["chamber_target"] == 65.0


class TestTheHotendBit:
    def test_bit_3_is_read_off_the_extruder_word(self) -> None:
        c = _client(model="H2D")
        c._update_state({"device": {"extruder": {"info": [{"id": 0, "info": 0b1000}, {"id": 1, "info": 0b0000}]}}})

        assert c.state.ext_has_nozzle == {0: True, 1: False}

    def test_a_machine_that_never_reports_it_says_nothing(self) -> None:
        """⚠️ Absent is not False. BS initialises ``m_has_nozzle`` to true
        because the A and P series cannot detect a hotend at all — so only a
        machine that reported the word may ever refuse a heat request."""
        c = _client()
        c._update_state({"bed_temper": 60})

        assert c.state.ext_has_nozzle == {}
