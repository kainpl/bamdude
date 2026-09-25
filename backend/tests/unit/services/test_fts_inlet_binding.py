"""Which Filament Track Switch inlet each AMS feeds (upstream 7a42e0a7).

With a switch fitted every AMS reports its extruder as 0xE ("routing is not
fixed") and sits on one of the switch's two inlets instead. BambuStudio reads
that inlet out of bits 24-27 of the same AMS ``info`` hex it parses for the type
and extruder: 0 = In-B, 1 = In-A (``DevFilaSystem.cpp``), and only when a switch
is installed — without one 0xE is an uninitialised unit and those bits carry
nothing. ``DevFilaSwitch::IsReady`` then asks every AMS for an inlet.
"""

from __future__ import annotations

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.utils.fila_switch import inlet_bindings, switch_ready

FTS_AUX = "20000000"  # aux bit 29 — BS's installation authority


def _info(extruder: int, inlet_bits: int = 0xF, ams_type: int = 1) -> str:
    return format((inlet_bits << 24) | (extruder << 8) | ams_type, "08X")


def _ams(*units: tuple[str, str]) -> dict:
    return {"ams": [{"id": ams_id, "info": info, "tray": []} for ams_id, info in units]}


@pytest.fixture
def client() -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TEST123", access_code="12345678")
    c.model = "H2C"
    return c


def _frame(client: BambuMQTTClient, *, fts: bool, ams: dict) -> None:
    body: dict = {"ams": ams}
    if fts:
        body["aux"] = FTS_AUX
        body["device"] = {"fila_switch": {"in": [-1, -1], "out": [0, 1]}}
    else:
        body["aux"] = "0"
    client._process_message({"print": body})


def test_the_inlet_is_read_on_the_same_frame_that_reports_the_switch(client):
    """The switch block and the AMS block arrive together; the binding must not
    depend on which of them is parsed first."""
    _frame(client, fts=True, ams=_ams(("0", _info(0xE, 1)), ("1", _info(0xE, 0))))

    assert inlet_bindings(client.state) == {"0": "A", "1": "B"}
    assert switch_ready(client.state) is True


def test_without_a_switch_the_bits_mean_nothing(client):
    _frame(client, fts=False, ams=_ams(("0", _info(0xE, 1))))

    assert inlet_bindings(client.state) == {}
    assert switch_ready(client.state) is False


def test_a_unit_not_yet_assigned_to_an_inlet_leaves_the_switch_not_ready(client):
    _frame(client, fts=True, ams=_ams(("0", _info(0xE, 1)), ("1", _info(0xE, 0xF))))

    assert inlet_bindings(client.state) == {"0": "A"}
    assert switch_ready(client.state) is False


def test_a_unit_back_on_a_real_extruder_drops_its_inlet(client):
    """BS resets the switcher position whenever the unit reports a real
    extruder id; a stale inlet would keep the switch 'ready' for a unit that is
    not on it."""
    _frame(client, fts=True, ams=_ams(("0", _info(0xE, 1))))
    assert inlet_bindings(client.state) == {"0": "A"}

    _frame(client, fts=True, ams=_ams(("0", _info(0))))

    assert inlet_bindings(client.state) == {}
    assert switch_ready(client.state) is False


def test_a_switch_with_no_ams_reported_yet_is_ready(client):
    """As in Studio: no AMS, no slot to load from, nothing to refuse."""
    _frame(client, fts=True, ams={"ams": []})

    assert switch_ready(client.state) is True


def test_the_ws_shaper_and_the_broadcast_key_carry_the_binding(client):
    from backend.app.main import _fts_status_key
    from backend.app.services.printer_manager import printer_state_to_dict

    _frame(client, fts=True, ams=_ams(("0", _info(0xE, 1))))
    before = _fts_status_key(client.state)
    payload = printer_state_to_dict(client.state, 1, "H2C")
    assert payload["ams_switch_inlet"] == {"0": "A"}
    assert payload["fila_switch"]["ready"] is True

    _frame(client, fts=True, ams=_ams(("0", _info(0xE, 0))))
    assert printer_state_to_dict(client.state, 1, "H2C")["ams_switch_inlet"] == {"0": "B"}
    # "Join IN-B" on the printer screen must reach the page.
    assert _fts_status_key(client.state) != before
