"""Fans that exist only in ``device.airduct`` (upstream #2576 / P2S-X2D kits).

The second auxiliary fan is never mirrored into a flat ``big_fanX_speed`` field.
Every consumer read the flat fields, so the fan was invisible — on the P2S it is
an add-on kit, on the X2D it ships from the factory.

The encoding is taken from BambuStudio's ``DevFan::ParseV3_0`` rather than
guessed, and the two facts that bite are pinned here: the raw part id is
shifted, and ``state`` is only its low 8 bits.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient, PrinterState, airduct_fan_controllable


def _client(model: str | None = None, **state_kwargs) -> BambuMQTTClient:
    c = BambuMQTTClient.__new__(BambuMQTTClient)
    c.serial_number = "01P00A000000000"
    # Needed since the fan inventory learned to synthesise the old protocol:
    # which fans a machine has is a per-model question. These tests are about
    # the wire protocol, so None is honest — it means "no model answer", and the
    # synthesis then offers only what every printer has.
    c.model = model
    c._sequence_id = 0
    c._client = MagicMock()
    c.state = PrinterState()
    for k, v in state_kwargs.items():
        setattr(c.state, k, v)
    return c


class TestPartDecoding:
    def test_the_raw_id_is_shifted_not_literal(self):
        """160 is part 10, not 160. Low 4 bits are the type, bits 4-11 the id."""
        c = _client()
        c._parse_airduct_parts({"parts": [{"id": 160, "state": 55, "range": 100 << 16}]})
        assert list(c.state.airduct_parts) == [10]
        assert c.state.airduct_parts[10]["type"] == 0  # a fan, not an air door

    def test_state_is_only_its_low_eight_bits(self):
        # Reading the whole word gives a "speed" in the thousands, which then
        # renders as a fan running at 2000 %.
        c = _client()
        c._parse_airduct_parts({"parts": [{"id": 160, "state": (7 << 8) | 42, "range": 0}]})
        assert c.state.airduct_parts[10]["state"] == 42

    def test_the_part_states_its_own_range(self):
        c = _client()
        c._parse_airduct_parts({"parts": [{"id": 160, "state": 0, "range": (100 << 16) | 20}]})
        part = c.state.airduct_parts[10]
        assert (part["range_start"], part["range_end"]) == (20, 100)

    def test_air_doors_share_the_list_and_are_kept_with_their_type(self):
        # type 1 is an air door. Parsing keeps it; the status route filters it,
        # so nothing here has to know what the UI wants.
        c = _client()
        c._parse_airduct_parts({"parts": [{"id": (3 << 4) | 1, "state": 1, "range": 0}]})
        assert c.state.airduct_parts[3]["type"] == 1

    def test_malformed_entries_are_skipped_without_losing_the_rest(self):
        c = _client()
        c._parse_airduct_parts(
            {"parts": [{"id": "nonsense"}, {"no_id": 1}, {"id": 160, "state": 30, "range": 100 << 16}]}
        )
        assert list(c.state.airduct_parts) == [10]


class TestPartialFrames:
    def test_a_frame_without_parts_does_not_retract_the_fans(self):
        """Bambu sends diff pushes constantly.

        Clearing on a frame that simply omits the key would retract a fan kit
        and make the tile flicker — the same latching reasoning as the
        nozzle-flow-type flags.
        """
        c = _client()
        c._parse_airduct_parts({"parts": [{"id": 160, "state": 30, "range": 100 << 16}]})
        c._parse_airduct_parts({"modeCur": 1})
        assert 10 in c.state.airduct_parts

    def test_an_empty_parts_list_is_also_treated_as_silence(self):
        c = _client()
        c._parse_airduct_parts({"parts": [{"id": 160, "state": 30, "range": 100 << 16}]})
        c._parse_airduct_parts({"parts": []})
        assert 10 in c.state.airduct_parts

    def test_a_frame_with_parts_replaces_them(self):
        c = _client()
        c._parse_airduct_parts({"parts": [{"id": 160, "state": 30, "range": 100 << 16}]})
        c._parse_airduct_parts({"parts": [{"id": 160, "state": 80, "range": 100 << 16}]})
        assert c.state.airduct_parts[10]["state"] == 80


class TestModeList:
    def test_mode_entries_shift_their_part_ids_the_same_way(self):
        c = _client()
        c._parse_airduct_parts({"modeList": [{"modeId": 1, "ctrl": [32], "off": [160]}]})
        assert c.state.airduct_modes[1] == {"ctrl": [2], "off": [10]}

    def test_a_fan_the_mode_forces_off_is_not_controllable(self):
        # The P2S's left aux fan is forced off in heating mode. The printer
        # accepts the command and ignores it, which reads as a broken control.
        state = PrinterState()
        state.airduct_mode = 1
        state.airduct_modes = {1: {"ctrl": [2], "off": [10]}}
        assert airduct_fan_controllable(state, 10) is False
        assert airduct_fan_controllable(state, 2) is True

    def test_an_unknown_mode_does_not_disable_anything(self):
        # A missing mode list must not disable a fan the hardware is reporting.
        state = PrinterState()
        state.airduct_mode = 7
        assert airduct_fan_controllable(state, 10) is True


class TestControlProtocol:
    """BS picks the wire command; we pick it the same way.

    ``FanControl.cpp::FanControlNew::command_control_fan``:
    new protocol AND airduct present → ``set_fan``; otherwise ``M106``.
    """

    @staticmethod
    def _published(c: BambuMQTTClient) -> dict:
        import json

        return json.loads(c._client.publish.call_args[0][1])["print"]

    def test_an_airduct_printer_on_the_new_protocol_gets_set_fan(self):
        c = _client(
            connected=True,
            enable_np=True,
            airduct_parts={10: {"state": 0, "range_start": 0, "range_end": 100, "type": 0}},
        )
        assert c.set_fan_speed(10, 60) is True
        cmd = self._published(c)
        assert cmd["command"] == "set_fan"
        assert cmd["fan_index"] == 10
        assert cmd["speed"] == 60  # set_fan is 0-100

    def test_an_older_printer_gets_m106_on_the_0_255_scale(self):
        c = _client(
            connected=True,
            enable_np=False,
            airduct_parts={2: {"state": 0, "range_start": 0, "range_end": 100, "type": 0}},
        )
        assert c.set_fan_speed(2, 100) is True
        cmd = self._published(c)
        assert cmd["command"] == "gcode_line"
        # The two protocols disagree about the scale; passing a percentage into
        # M106 would run the fan at 40 % of what was asked.
        assert cmd["param"].strip() == "M106 P2 S255"

    def test_the_speed_is_clamped_to_the_range_the_part_declared(self):
        c = _client(
            connected=True,
            enable_np=True,
            airduct_parts={10: {"state": 0, "range_start": 20, "range_end": 80, "type": 0}},
        )
        c.set_fan_speed(10, 100)
        assert self._published(c)["speed"] == 80

    def test_a_fan_the_mode_owns_is_refused_rather_than_sent(self):
        c = _client(
            connected=True,
            enable_np=True,
            airduct_mode=1,
            airduct_modes={1: {"ctrl": [], "off": [10]}},
            airduct_parts={10: {"state": 0, "range_start": 0, "range_end": 100, "type": 0}},
        )
        assert c.set_fan_speed(10, 50) is False
        c._client.publish.assert_not_called()

    def test_a_disconnected_client_sends_nothing(self):
        c = _client(connected=False)
        assert c.set_fan_speed(10, 50) is False
        c._client.publish.assert_not_called()


class TestLabels:
    """The label is per model AND per mode — see printer_configs.airduct_fan_label."""

    def test_part_10_is_the_left_fan_on_a_p2s_and_the_right_one_on_an_x2d(self):
        """The load-bearing case, and the one upstream gets wrong.

        They hardcode "part 10 = Left Auxiliary" for both models. Correct on the
        P2S their contributor measured; the X2D mirrors it, and is named in the
        same commit title. If this test ever collapses to one answer, somebody
        has replaced the config lookup with a constant.
        """
        from backend.app.utils.printer_configs import airduct_fan_label

        assert airduct_fan_label("P2S", mode=0, sub_mode=-1, part_id=10)[1] == "Left(Aux)"
        assert airduct_fan_label("X2D", mode=0, sub_mode=-1, part_id=10)[1] == "Right(Aux)"

    def test_the_sub_mode_picks_between_two_names_for_one_part(self):
        # X2D part 10 in cooling: subMode 0 → Right(Aux), 1 → Right(Filter).
        from backend.app.utils.printer_configs import airduct_fan_label

        assert airduct_fan_label("X2D", mode=0, sub_mode=1, part_id=10)[1] == "Right(Filter)"
        assert airduct_fan_label("X2D", mode=0, sub_mode=0, part_id=10)[1] == "Right(Aux)"

    def test_the_mode_changes_the_name_of_the_same_fan(self):
        from backend.app.utils.printer_configs import airduct_fan_label

        assert airduct_fan_label("P2S", mode=0, sub_mode=-1, part_id=2)[1] == "Right(Aux)"
        assert airduct_fan_label("P2S", mode=1, sub_mode=-1, part_id=2)[1] == "Right(Filter)"

    def test_mode_minus_one_is_the_any_mode_fallback(self):
        # How X1/P1S name part 3 "Chamber".
        from backend.app.utils.printer_configs import airduct_fan_label

        assert airduct_fan_label("X1C", mode=0, sub_mode=-1, part_id=3) == ("chamber", "Chamber")

    def test_a_recognised_label_carries_an_i18n_key_and_an_unknown_one_does_not(self):
        from backend.app.utils.printer_configs import airduct_fan_label

        assert airduct_fan_label("P2S", mode=0, sub_mode=-1, part_id=10)[0] == "leftAux"
        # A model whose config names nothing for this part.
        assert airduct_fan_label("H2D", mode=0, sub_mode=-1, part_id=10) == (None, None)

    @pytest.mark.parametrize("model", ["P2S", "X2D"])
    def test_every_label_these_models_use_is_translatable(self, model):
        """No silent English on the two models this feature targets.

        The fallback exists for a config we have not seen yet, not as the normal
        path — if a resync adds a word, this fails and the key gets added.
        """
        from backend.app.utils.printer_configs import airduct_fan_label

        for mode in (0, 1):
            for part_id in (2, 10):
                key, raw = airduct_fan_label(model, mode=mode, sub_mode=0, part_id=part_id)
                assert raw, f"{model} mode={mode} part={part_id} unnamed"
                assert key, f"{model} mode={mode} part={part_id} → {raw!r} has no i18n key"
