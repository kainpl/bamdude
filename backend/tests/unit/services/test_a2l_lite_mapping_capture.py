"""A print from an A2L's AMS Lite charges the Lite's spool (upstream be0ad787b).

The A2L's 4-slot AMS Lite is physical unit 16 on the wire and unit 6 (global trays 24-27) everywhere
inside BamDude. A ``project_file`` names a Lite tray as its LOCAL slot 0-3 in the flat ``ams_mapping`` and
as ``{ams_id: 16, slot_id: n}`` in ``ams_mapping2`` -- our own dispatch included. The capture kept the flat
list, so the mapping the trackers read said "global tray 1" for Lite slot 2: on a Lite-only A2L the grams
went to nobody, and for Lite slot 1 the flat ``0`` read as "the external holder in disguise" and charged
the external holder's spool. The captured mapping outranks the queue row's correct copy, so the dispatched
mapping rule could not help: the dispatched mapping was the wrong one.

The capture now names the Lite tray by its global id from the command's own ``ams_mapping2``. What goes out
on the wire is not touched.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient, resolve_lite_trays_in_mapping
from backend.app.services.usage_tracker import _active_sessions
from backend.app.utils.tray_split import terminal_snapshot_fields
from backend.tests.unit.services.test_partial_print_split import (  # noqa: F401 — fixtures
    _clean_memory,
    history,
    run_internal,
    session,
    stage,
)


def lite(slot: int) -> dict:
    return {"ams_id": 16, "slot_id": slot}


class TestTheLiteTrayGetsItsGlobalId:
    @pytest.mark.parametrize("slot", [0, 1, 2, 3])
    def test_every_lite_slot(self, slot):
        assert resolve_lite_trays_in_mapping([slot], [lite(slot)]) == [24 + slot]

    def test_a_regular_ams_tray_beside_it_is_left_exactly_as_captured(self):
        assert resolve_lite_trays_in_mapping([2, 1], [{"ams_id": 0, "slot_id": 2}, lite(1)]) == [2, 25]

    def test_an_external_holder_beside_it_is_left_for_its_own_resolver(self):
        assert resolve_lite_trays_in_mapping([-1, 3], [{"ams_id": 255, "slot_id": 0}, lite(3)]) == [-1, 27]

    def test_a_flat_tray_that_is_not_the_slot_the_detail_names_stands(self):
        """Only the measured form is read: the flat entry IS the local slot. Anything else is not ours to guess."""
        assert resolve_lite_trays_in_mapping([5], [lite(1)]) == [5]
        assert resolve_lite_trays_in_mapping([-1], [lite(1)]) == [-1]

    @pytest.mark.parametrize("slot", [4, -1, 255, None, "1", True])
    def test_a_slot_out_of_range_stands(self, slot):
        assert resolve_lite_trays_in_mapping([1], [{"ams_id": 16, "slot_id": slot}]) == [1]

    @pytest.mark.parametrize("detail", [None, "x", [], [lite(1), lite(2)], [None], [{"slot_id": 1}]])
    def test_without_a_usable_detail_the_flat_list_stands(self, detail):
        assert resolve_lite_trays_in_mapping([1], detail) == [1]

    def test_something_that_is_no_list_is_returned_untouched(self):
        assert resolve_lite_trays_in_mapping(None, [lite(1)]) is None

    def test_a_mapping_with_no_lite_tray_is_the_very_same_object(self):
        flat = [0, 5, 11]
        assert resolve_lite_trays_in_mapping(flat, [{"ams_id": 0, "slot_id": 0}] * 3) is flat


def mqtt_client(model):
    client = BambuMQTTClient(ip_address="127.0.0.1", serial_number="OFFLINE-LITE", access_code="synthetic", model=model)
    client._client = MagicMock()
    client.state.connected = True
    client.state.raw_data["ams"] = {"ams": [{"id": "0"}]}
    return client


def dispatched(model, mapping):
    """What OUR ``start_print`` publishes for ``mapping``, and what the capture then holds."""
    client = mqtt_client(model)
    client.start_print("test.3mf", ams_mapping=mapping)
    sent = json.loads(client._client.publish.call_args[0][1])
    client._handle_project_file_command(sent)
    return sent["print"], client._captured_ams_mapping


class TestOurOwnDispatchReadsBackWhatThePlannerChose:
    @pytest.mark.parametrize("tray", [24, 25, 26, 27])
    def test_an_a2l_lite_tray(self, tray):
        """Was ``[tray - 24]``: the local slot, read by the trackers as a tray of AMS 0."""
        sent, captured = dispatched("A2L", [tray])

        assert sent["ams_mapping"] == [tray - 24]  # the wire is what the firmware was measured to take
        assert sent["ams_mapping2"] == [lite(tray - 24)]
        assert captured == [tray]

    def test_the_printers_echo_is_read_the_same_way(self):
        client = mqtt_client("A2L")
        echo = {
            "print": {
                "command": "project_file",
                "result": "SUCCESS",
                "ams_mapping": [1, 3],
                "ams_mapping2": [lite(1), lite(3)],
            }
        }

        client._handle_project_file_command(echo, source="report")

        assert client._captured_ams_mapping == [25, 27]

    def test_a_lite_tray_and_the_external_holder_in_one_print(self):
        sent, captured = dispatched("A2L", [26, 254])

        assert sent["ams_mapping"] == [2, -1]
        assert captured == [26, 254]


class TestEveryOtherMappingIsExactlyAsBefore:
    """GUARD: the wire payload and the captured mapping of everything that is not an AMS Lite."""

    @pytest.mark.parametrize(
        ("model", "mapping", "flat", "detail", "read_back"),
        [
            (
                "P1S",
                [0, 5, 11],
                [0, 5, 11],
                [{"ams_id": 0, "slot_id": 0}, {"ams_id": 1, "slot_id": 1}, {"ams_id": 2, "slot_id": 3}],
                [0, 5, 11],
            ),
            ("P1S", [254], [-1], [{"ams_id": 255, "slot_id": 0}], [254]),
            ("X2D", [254, 255], [-1, -1], [{"ams_id": 254, "slot_id": 0}, {"ams_id": 255, "slot_id": 0}], [254, 255]),
            ("X2D", [0, 255], [0, -1], [{"ams_id": 0, "slot_id": 0}, {"ams_id": 255, "slot_id": 0}], [0, 255]),
            ("H2D", [128, 1], [128, 1], [{"ams_id": 128, "slot_id": 0}, {"ams_id": 0, "slot_id": 1}], [128, 1]),
            ("X1C", [-1, 2], [-1, 2], [{"ams_id": 255, "slot_id": 255}, {"ams_id": 0, "slot_id": 2}], [-1, 2]),
            # AMS 4 slot 0-3 are global trays 16-19: a flat 16 is a tray id, never the Lite's unit id.
            ("X1C", [16, 17], [16, 17], [{"ams_id": 4, "slot_id": 0}, {"ams_id": 4, "slot_id": 1}], [16, 17]),
        ],
    )
    def test_wire_and_capture(self, model, mapping, flat, detail, read_back):
        sent, captured = dispatched(model, mapping)

        assert sent["ams_mapping"] == flat
        assert sent["ams_mapping2"] == detail
        assert captured == read_back


# ── The figure: 40 g from the Lite, external holder loaded too ─────────────────

ESTIMATE = [{"slot_id": 1, "used_g": 40.0, "type": "PLA", "color": "#000000"}]
COMPLETED = {
    "status": "completed",
    "last_layer_num": 200,
    "last_progress": 100.0,
    **terminal_snapshot_fields(layer_num=200, total_layers=200, progress=100.0),
}


def a2l_state():
    state = MagicMock()
    state.raw_data = {
        "ams": [
            {
                "id": 6,
                "tray": [{"id": n, "tray_type": "PLA", "tray_color": "000000FF", "state": 11} for n in range(4)],
            }
        ],
        "vt_tray": [{"id": 254, "tray_type": "PLA", "tray_color": "FFFFFFFF"}],
    }
    state.tray_now = 255
    state.last_loaded_tray = -1
    state.tray_change_log = []
    pm = MagicMock()
    pm.get_status.return_value = state
    return pm


class TestTheLiteSpoolIsCharged:
    async def test_lite_slot_2_charges_the_spool_in_lite_slot_2(self, db_session, stage):  # noqa: F811
        """Was nothing at all: tray 1 is AMS 0 slot 2, which a Lite-only A2L does not have."""
        _sent, captured = dispatched("A2L", [25])
        _active_sessions[stage.printer.id] = session(
            stage.printer.id,
            spool_assignments={(255, 0): stage.a, (6, 1): stage.b},
            started_at=None,
            ams_mapping=captured,
        )

        await run_internal(db_session, stage.printer, stage.archive, COMPLETED, a2l_state(), usage=ESTIMATE)

        assert await history(db_session) == {(stage.b, 40.0)}

    async def test_lite_slot_1_charges_the_lite_and_not_the_external_holder(self, db_session, stage):  # noqa: F811
        """Was {(external holder's spool, 40.0)}: a flat 0 on a machine without AMS 0 reads as the holder."""
        _sent, captured = dispatched("A2L", [24])
        _active_sessions[stage.printer.id] = session(
            stage.printer.id,
            spool_assignments={(255, 0): stage.a, (6, 0): stage.b},
            started_at=None,
            ams_mapping=captured,
        )

        await run_internal(db_session, stage.printer, stage.archive, COMPLETED, a2l_state(), usage=ESTIMATE)

        assert await history(db_session) == {(stage.b, 40.0)}

    async def test_a_regular_ams_print_is_charged_exactly_as_before(self, db_session, stage):  # noqa: F811
        """GUARD 1: a P1S print from AMS 0 slot 2 -- same mapping read back, same spool, same grams."""
        _sent, captured = dispatched("P1S", [1])
        assert captured == [1]
        _active_sessions[stage.printer.id] = session(
            stage.printer.id,
            spool_assignments={(0, 0): stage.a, (0, 1): stage.b},
            started_at=None,
            ams_mapping=captured,
        )
        state = a2l_state()
        state.get_status.return_value.raw_data = {
            "ams": [{"id": 0, "tray": [{"id": n, "tray_type": "PLA", "state": 11} for n in range(4)]}]
        }

        await run_internal(db_session, stage.printer, stage.archive, COMPLETED, state, usage=ESTIMATE)

        assert await history(db_session) == {(stage.b, 40.0)}
