"""Moving the head, the bed and the extruder by hand.

BambuStudio settles all four axes in one function, ``DevAxis::Ctrl_Axis``, and
it has more shape than it looks:

⚠️ **Y and Z invert on non-CoreXY machines; X and E do not.** BS:
``if (!IsArchCoreXY()) { if (axis == "Y" || axis == "Z") value = -value; }``. On
a bed-slinger the Z axis carries the toolhead rather than the bed, so the same
command means the opposite motion — the crash in upstream #1334. The Y half only
became visible once Y was controllable at all, which it was not here before.

⚠️ **The extruder gets no endstop wrapper.** X/Y/Z are bracketed by
``M211``/``push_ref_mode``; E is a bare ``M83`` + ``G0``. That is BS's shape, not
an omission — there are no soft endstops on an extruder and no reference frame a
jog could disturb.

⚠️ **The MQTT path cannot carry a distance.** ``xyz_ctrl`` has room for a
direction and a coarse/fine ``mode`` (``abs(value) >= 10``) and nothing else, so
3 mm and 9 mm are the same request on a machine that speaks it. Adopting it is
therefore not a transport swap; it narrows what the API can promise.

⚠️ **A ``home_flag`` of exactly 0 means every axis IS homed.** BS writes each
accessor as ``m_home_flag == 0 ? true : (bit)`` — zero is the "nothing reported"
sentinel, and reading it as "nothing homed" would refuse a jog on every printer
that omits the field.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _client(model: str = "X1C") -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="1.2.3.4", serial_number="S1", access_code="12345678", model=model)
    c._client = MagicMock()
    c.state.connected = True
    return c


def _published(c: BambuMQTTClient) -> dict:
    return json.loads(c._client.publish.call_args[0][1])["print"]


def _gcode(c: BambuMQTTClient) -> list[str]:
    return _published(c)["param"].strip().splitlines()


class TestTheGcodeShapeIsBsOwn:
    @pytest.mark.parametrize("axis", ["X", "Y", "Z"])
    def test_a_linear_axis_is_bracketed_by_the_endstop_dance(self, axis: str) -> None:
        c = _client()
        assert c.move_axis(axis, 10) is True

        lines = _gcode(c)
        assert lines[0] == "M211 S"
        assert lines[1] == "M211 X1 Y1 Z1"
        assert lines[2] == "M1002 push_ref_mode"
        assert lines[3] == "G91"
        assert lines[-2] == "M1002 pop_ref_mode"
        assert lines[-1] == "M211 R"

    def test_the_extruder_gets_no_wrapper_at_all(self) -> None:
        """⚠️ Two lines, and that is correct: an extruder has no soft endstops
        to push and no reference frame to protect."""
        c = _client()
        c.move_axis("E", -10)

        assert _gcode(c) == ["M83", "G0 E-10.0 F900"]

    @pytest.mark.parametrize(("axis", "speed"), [("X", 3000), ("Y", 3000), ("Z", 900), ("E", 900)])
    def test_each_axis_moves_at_the_rate_bs_gives_it(self, axis: str, speed: int) -> None:
        """The toolhead runs more than three times faster than the bed, and the
        extruder shares the bed's rate rather than the toolhead's."""
        c = _client()
        c.move_axis(axis, 10)

        # The move is the last line for E (no wrapper) and line 4 for the rest.
        move_line = _gcode(c)[-1] if axis == "E" else _gcode(c)[4]
        assert f"F{speed}" in move_line

    def test_an_axis_bs_does_not_know_is_refused(self) -> None:
        c = _client()

        assert c.move_axis("A", 10) is False
        c._client.publish.assert_not_called()


class TestTheInversion:
    @pytest.mark.parametrize("axis", ["Y", "Z"])
    def test_a_bed_slinger_flips_y_and_z(self, axis: str) -> None:
        c = _client(model="A1")
        c.move_axis(axis, 10)

        assert f"G1 {axis}-10.0" in _gcode(c)[4]

    @pytest.mark.parametrize("axis", ["Y", "Z"])
    def test_a_corexy_machine_does_not(self, axis: str) -> None:
        c = _client(model="X1C")
        c.move_axis(axis, 10)

        assert f"G1 {axis}10.0" in _gcode(c)[4]

    def test_x_is_never_flipped(self) -> None:
        """⚠️ Only Y and Z. X reads the same on both frames."""
        c = _client(model="A1")
        c.move_axis("X", 10)

        assert "G1 X10.0" in _gcode(c)[4]

    def test_the_extruder_is_never_flipped(self) -> None:
        """A retract is a retract on every machine."""
        c = _client(model="A1")
        c.move_axis("E", -10)

        assert _gcode(c)[1] == "G0 E-10.0 F900"


class TestTheMqttPath:
    def test_it_replaces_the_gcode_when_the_printer_offers_it(self) -> None:
        c = _client()
        c.state.print_option_support["mqtt_axis_ctrl"] = True
        c.move_axis("X", 10)

        p = _published(c)
        assert p["command"] == "xyz_ctrl"
        assert p["axis"] == "X"

    @pytest.mark.parametrize(("distance", "mode"), [(1, 0), (9.9, 0), (10, 1), (200, 1)])
    def test_distance_collapses_to_a_coarse_flag(self, distance: float, mode: int) -> None:
        """⚠️ The protocol's own limit. 3 mm and 9 mm are indistinguishable
        here, and so are 10 mm and 200 mm."""
        c = _client()
        c.state.print_option_support["mqtt_axis_ctrl"] = True
        c.move_axis("X", distance)

        assert _published(c)["mode"] == mode

    def test_the_inversion_still_applies(self) -> None:
        """BS flips ``dir`` on this path too — the frame does not change with
        the transport."""
        c = _client(model="A1")
        c.state.print_option_support["mqtt_axis_ctrl"] = True
        c.move_axis("Y", 10)

        assert _published(c)["dir"] == -1


class TestHoming:
    def test_without_the_bit_it_is_a_bare_g28(self) -> None:
        """⚠️ Bare, never ``G28 Z``: the partial form skips the toolhead park
        and drives the bed into the head on the machines that home Z upward."""
        c = _client()
        assert c.home_axes() is True

        assert _gcode(c) == ["G28"]

    def test_with_the_bit_it_is_back_to_center(self) -> None:
        c = _client()
        c.state.print_option_support["mqtt_homing"] = True
        c.home_axes()

        assert _published(c)["command"] == "back_to_center"


class TestExtruderControl:
    def test_the_new_protocol_names_the_extruder(self) -> None:
        c = _client(model="H2D")
        c.state.enable_np = True
        assert c.extruder_control(-10, extruder_index=1) is True

        p = _published(c)
        assert p["command"] == "set_extrusion_length"
        assert p["extruder_index"] == 1
        assert p["length"] == -10

    def test_the_fallback_cannot_name_it(self) -> None:
        """⚠️ ``G0 E`` acts on whichever extruder is active, so on a dual-nozzle
        machine the g-code path simply cannot address the second one — the same
        gap the nozzle temperature had."""
        c = _client(model="H2D")
        c.extruder_control(-10, extruder_index=1)

        assert _gcode(c) == ["M83", "G0 E-10.0 F900"]

    def test_length_is_whole_millimetres_on_the_wire(self) -> None:
        """BS casts to int before publishing."""
        c = _client()
        c.state.enable_np = True
        c.extruder_control(-10.7)

        assert isinstance(_published(c)["length"], int)


class TestTheHomedBits:
    def test_bits_zero_one_two_are_x_y_z(self) -> None:
        c = _client()
        c._update_state({"home_flag": 0b011})

        assert c.state.axis_at_home == {"x": True, "y": True, "z": False}

    def test_zero_means_all_homed_not_none(self) -> None:
        """⚠️ The sentinel. Reading 0 as "nothing homed" would lock every
        printer that omits the field out of jogging."""
        c = _client()
        c._update_state({"home_flag": 0})

        assert c.state.axis_at_home == {"x": True, "y": True, "z": True}

    def test_the_default_before_any_report_is_homed(self) -> None:
        """Same reasoning as the sentinel: absence is not evidence of a printer
        being off its home."""
        assert _client().state.axis_at_home == {"x": True, "y": True, "z": True}


class TestEnableNp:
    def test_all_four_keys_turn_it_on(self) -> None:
        c = _client()
        c._update_state({"cfg": "0", "fun": "0", "aux": "0", "stat": "0"})

        assert c.state.enable_np is True

    def test_three_of_four_do_not(self) -> None:
        c = _client()
        c._update_state({"cfg": "0", "fun": "0", "aux": "0"})

        assert c.state.enable_np is False

    def test_it_is_sticky_where_bs_recomputes(self) -> None:
        """⚠️ A deliberate divergence. BS re-runs ``check_enable_np`` per message
        and lets a sparse push switch it back off, which would make the extruder
        command flip protocol between one message and the next. Which protocol a
        machine speaks belongs to its firmware, not to the message that happened
        to arrive."""
        c = _client()
        c._update_state({"cfg": "0", "fun": "0", "aux": "0", "stat": "0"})
        c._update_state({"bed_temper": 60})

        assert c.state.enable_np is True


class TestDisableSteppers:
    def test_it_is_m84(self) -> None:
        c = _client()
        assert c.disable_steppers() is True

        assert _gcode(c) == ["M84"]
