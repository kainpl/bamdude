"""An AMS unplugged while BamDude runs leaves the card and the Virtual Printer (upstream ad62d1063, #3356).

The AMS merge only ever added and updated units, so an unplugged AMS stayed on the printer card -- and
in the slicer, through a Virtual Printer -- until a restart. A unit is dropped only on the printer's own
word, in the same push: an AMS 0-3 whose ``ams_exist_bits`` bit is clear, or every unit when the push
lists none and no bit is set. The bit layout is upstream's measurement (an X1C: unit 1 -> "2", unplugged
-> "0" with an empty list; an H2C: units 0-2 and HT 128 -> "17"); nothing here was captured on our
printers. A partial list, a push without the bits, an AMS-HT or AMS Lite beside other units and the
shutdown shape keep their units.

What happens to the spools of such an AMS is ours (owner decision Р9): they stay assigned --
``tests/integration/test_unplugged_ams_keeps_its_spools.py``.
"""

from __future__ import annotations

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient, unit_reported_absent
from backend.app.services.virtual_printer.mqtt_bridge import _merge_ams_dict


@pytest.fixture
def client() -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="192.0.2.10", access_code="fixture", serial_number="Synthetic")
    c.state.connected = True
    return c


def _unit(unit_id, trays=4):
    return {"id": str(unit_id), "tray": [{"id": str(t), "tray_type": "PLA"} for t in range(trays)]}


def _push(units, bits):
    return {"ams": units, "ams_exist_bits": bits, "tray_now": "255", "power_on_flag": False}


def _unit_ids(client):
    return sorted((str(u["id"]) for u in client.state.raw_data["ams"]), key=int)


class TestThePredicate:
    @pytest.mark.parametrize(
        "ams_id, listed, bits, gone",
        [
            (1, [], "0", True),  # nothing listed, no bit: every unit is gone
            (128, [], "0", True),
            (1, [_unit(0)], "1", True),  # its own bit is clear
            (1, [_unit(0)], "3", False),  # its bit is set: a partial list
            (128, [_unit(0)], "1", False),  # an AMS-HT's bit is not read
            (6, [_unit(0)], "1", False),  # nor an AMS Lite's
            (1, [_unit(0)], None, False),  # the push says nothing about units
            (1, [_unit(0)], "zz", False),
            (1, [_unit(0)], "0", True),
            (128, [_unit(0)], "0", False),  # all-zero beside a listed unit: the shutdown shape
        ],
    )
    def test_it(self, ams_id, listed, bits, gone):
        assert unit_reported_absent(ams_id, listed, bits) is gone


class TestTheCard:
    def test_unplug_and_replug(self, client):
        client._handle_ams_data(_push([_unit(1)], "2"))

        client._handle_ams_data(_push([], "0"))
        assert _unit_ids(client) == []

        client._handle_ams_data(_push([_unit(1)], "2"))
        assert _unit_ids(client) == ["1"]

    def test_a_unit_whose_bit_is_cleared_goes(self, client):
        client._handle_ams_data(_push([_unit(0), _unit(1), _unit(2), _unit(128, 1)], "17"))

        client._handle_ams_data(_push([_unit(0), _unit(2), _unit(128, 1)], "15"))

        assert _unit_ids(client) == ["0", "2", "128"]

    def test_a_partial_list_keeps_the_units_it_leaves_out(self, client):
        client._handle_ams_data(_push([_unit(0), _unit(1), _unit(2), _unit(128, 1)], "17"))

        client._handle_ams_data(_push([_unit(0)], "17"))

        assert _unit_ids(client) == ["0", "1", "2", "128"]

    def test_an_ams_ht_beside_other_units_is_kept(self, client):
        client._handle_ams_data(_push([_unit(0), _unit(128, 1)], "11"))

        client._handle_ams_data(_push([_unit(0)], "1"))

        assert _unit_ids(client) == ["0", "128"]

    def test_the_last_ams_ht_goes_with_everything_else(self, client):
        client._handle_ams_data(_push([_unit(128, 1)], "10"))

        client._handle_ams_data(_push([], "0"))

        assert _unit_ids(client) == []

    def test_a_push_without_the_bits_changes_nothing(self, client):
        client._handle_ams_data(_push([_unit(0)], "1"))

        client._handle_ams_data({"ams": []})

        assert _unit_ids(client) == ["0"]

    def test_a_push_that_lists_no_units_at_all_changes_nothing(self, client):
        """A presence-only push carries no ``ams`` list: "no list" is not "an empty list"."""
        client._handle_ams_data(_push([_unit(0)], "1"))

        client._handle_ams_data({"tray_exist_bits": "0", "ams_exist_bits": "0", "power_on_flag": False})

        assert _unit_ids(client) == ["0"]

    def test_the_unplugged_unit_is_reported_as_an_ams_change(self, client):
        """The callback that keeps assignments and the card in step must hear of it."""
        client._handle_ams_data(_push([_unit(1)], "2"))
        heard = []
        client.on_ams_change = heard.append

        client._handle_ams_data(_push([], "0"))

        assert heard == [[]]


class TestTheVirtualPrintersView:
    PREV = {"ams": [{"id": "1", "tray": [{"id": "0", "tray_type": "PLA"}]}], "ams_exist_bits": "2"}

    def test_an_unplugged_unit_leaves_the_slicers_view(self):
        assert _merge_ams_dict(self.PREV, {"ams": [], "ams_exist_bits": "0"})["ams"] == []

    def test_a_partial_push_keeps_it(self):
        merged = _merge_ams_dict(self.PREV, {"ams": [{"id": "0", "tray": []}], "ams_exist_bits": "3"})
        assert sorted(u["id"] for u in merged["ams"]) == ["0", "1"]

    def test_a_push_without_the_bits_keeps_it(self):
        assert [u["id"] for u in _merge_ams_dict(self.PREV, {"ams": []})["ams"]] == ["1"]

    def test_a_status_only_push_keeps_it(self):
        assert [u["id"] for u in _merge_ams_dict(self.PREV, {"ams_exist_bits": "0"})["ams"]] == ["1"]
