"""A calibration entry's nozzle comes from the envelope, not from a guess (upstream #1748).

The printer puts ``nozzle_diameter`` on the ``extrusion_cali_get`` envelope. A
per-filament entry carries ``setting_id``, ``filament_id``, ``name``,
``k_value``, ``n_coef`` and ``cali_idx`` — and, on every single-nozzle model,
nothing else. Four construction sites read the field off the *entry* and
defaulted to ``"0.4"``, so on a 0.6 or 0.8 mm machine every K-profile was
reported as 0.4 mm while the correct value sat unread on the envelope two lines
away.

It never reproduced on H2D: that firmware repeats the field per entry. Which is
why the entry still wins when it is present — these tests pin both directions.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _client() -> BambuMQTTClient:
    c = BambuMQTTClient.__new__(BambuMQTTClient)
    c.serial_number = "01P00A000000000"
    c._kprofile_waiters = {}
    c._last_kprofiles_hash = None
    c.on_kprofiles_changed = None
    c._loop = None
    c.state = MagicMock()
    c.state.kprofiles = []
    return c


def _entry(**over) -> dict:
    base = {
        "cali_idx": 3,
        "name": "PLA Basic @0.6",
        "filament_id": "GFA00",
        "setting_id": "GFSA00",
        "k_value": "0.021000",
        "n_coef": "1.400000",
        "extruder_id": 0,
    }
    base.update(over)
    return base


class TestResolver:
    """The resolver itself — three inputs, three answers."""

    def test_the_entry_wins_when_it_names_a_diameter(self):
        # H2D firmware repeats the field per entry; that is the authoritative
        # value for that row and must not be overridden by the envelope.
        assert BambuMQTTClient._entry_nozzle_diameter({"nozzle_diameter": "0.8"}, {"nozzle_diameter": "0.4"}) == "0.8"

    def test_the_envelope_answers_when_the_entry_is_silent(self):
        assert BambuMQTTClient._entry_nozzle_diameter({}, {"nozzle_diameter": "0.6"}) == "0.6"

    @pytest.mark.parametrize("empty", [None, ""])
    def test_an_empty_entry_value_is_silence_not_an_answer(self, empty):
        # A key present with an empty value is what a firmware that "sends the
        # field" but has nothing to put in it looks like. ``.get(key, default)``
        # would return "" here and skip the envelope entirely.
        assert BambuMQTTClient._entry_nozzle_diameter({"nozzle_diameter": empty}, {"nozzle_diameter": "0.6"}) == "0.6"

    def test_a_payload_that_names_no_diameter_anywhere_still_yields_one(self):
        # The floor. ``KProfile.nozzle_diameter`` is not optional, so there has
        # to be an answer — but it is now reached only when the printer told us
        # nothing, instead of on every entry from every single-nozzle machine.
        assert BambuMQTTClient._entry_nozzle_diameter({}, {}) == "0.4"


class TestKProfileResponse:
    """``_handle_kprofile_response`` — both branches parse the same way."""

    def test_a_broadcast_takes_the_envelopes_nozzle(self):
        c = _client()
        c._handle_kprofile_response({"nozzle_diameter": "0.6", "filaments": [_entry()]})
        assert [p.nozzle_diameter for p in c.state.kprofiles] == ["0.6"]

    def test_a_matched_reply_takes_the_envelopes_nozzle_too(self):
        # The two branches are separate code paths with separate KProfile()
        # constructions. Fixing one and not the other would leave a printer
        # reporting 0.6 unprompted and 0.4 when asked.
        import asyncio

        c = _client()
        c._kprofile_waiters["7"] = (asyncio.Event(), "0.6", None)
        c._handle_kprofile_response({"nozzle_diameter": "0.6", "sequence_id": "7", "filaments": [_entry()]})
        assert [p.nozzle_diameter for p in c.state.kprofiles] == ["0.6"]

    def test_mixed_entries_are_resolved_row_by_row(self):
        # A dual-nozzle payload: one row states its own diameter, the other
        # relies on the envelope. Resolving once for the whole batch would get
        # one of them wrong whichever value it picked.
        c = _client()
        c._handle_kprofile_response(
            {
                "nozzle_diameter": "0.4",
                "filaments": [_entry(nozzle_diameter="0.8", extruder_id=1), _entry()],
            }
        )
        assert [p.nozzle_diameter for p in c.state.kprofiles] == ["0.8", "0.4"]


class TestTypedHistoryAndResults:
    """The two float-typed parse paths — same bug, different dataclass."""

    def test_history_entries_take_the_envelopes_nozzle(self):
        c = _client()
        c._handle_extrusion_cali_history({"nozzle_diameter": "0.6", "filaments": [_entry()]})
        assert [e.nozzle_diameter for e in c.state.extrusion_cali_history] == [0.6]

    def test_auto_cali_results_take_the_envelopes_nozzle(self):
        c = _client()
        c._handle_extrusion_cali_get_result(
            {
                "nozzle_diameter": "0.8",
                "filaments": [_entry(tray_id=0, ams_id=0, slot_id=0, confidence=1)],
            }
        )
        assert [r.nozzle_diameter for r in c.state.extrusion_cali_results] == [0.8]


class TestLiveFlowTypeFlags:
    """The live half of BS's nozzle-flow-type gate, latched (#1748).

    ``is_enable_np`` is the new-protocol quartet (cfg+fun+aux+stat);
    ``has_extra_flow_type`` is a nozzle frame that also carried ``flag3``.
    """

    @staticmethod
    def _parsing_client() -> BambuMQTTClient:
        from backend.app.services.bambu_mqtt import PrinterState

        c = _client()
        c.state = PrinterState()
        return c

    def test_the_new_protocol_quartet_latches_enable_np(self):
        c = self._parsing_client()
        c._latch_flow_type_flags({"cfg": "0x0", "fun": "0x0", "aux": "0", "stat": "0"})
        assert c.state.enable_np is True

    def test_three_of_the_four_is_not_the_quartet(self):
        c = self._parsing_client()
        c._latch_flow_type_flags({"cfg": "0x0", "fun": "0x0", "aux": "0"})
        assert c.state.enable_np is False

    def test_a_nozzle_frame_with_flag3_latches_extra_flow_type(self):
        c = self._parsing_client()
        c._latch_flow_type_flags({"nozzle_diameter": "0.4", "nozzle_type": "hardened_steel", "flag3": 0})
        assert c.state.has_extra_flow_type is True

    def test_a_nozzle_frame_without_flag3_does_not(self):
        c = self._parsing_client()
        c._latch_flow_type_flags({"nozzle_diameter": "0.4", "nozzle_type": "hardened_steel"})
        assert c.state.has_extra_flow_type is False

    def test_a_later_partial_push_does_not_retract_the_capability(self):
        """The reason both are latched rather than re-evaluated per frame.

        Our pushes are frequently partial — a frame carrying only
        ``gcode_state`` would otherwise retract a capability the printer has,
        and the Flow Type field would appear and disappear as frames arrive.
        """
        c = self._parsing_client()
        c._latch_flow_type_flags({"cfg": "0x0", "fun": "0x0", "aux": "0", "stat": "0"})
        c._latch_flow_type_flags({"gcode_state": "RUNNING"})
        assert c.state.enable_np is True
