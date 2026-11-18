"""``raw_data["ams"]`` is the merged unit list, whatever frame arrives first.

A printer's first frame after a connect may be a partial AMS update -- status fields only, no unit list
and no ``tray_exist_bits``. The AMS handler returns early on it and stores nothing, and the state update
then kept the frame's own ``ams`` dict under ``raw_data["ams"]``. Every reader that walks the units got
the dict's keys instead (``'str' object has no attribute 'get'`` in the status-effect key, on every
status change), and the handler itself failed on the next full frame, so the state never recovered
until a restart (farm log, 2026-11-18, an A1 mini without an AMS after a flapping connect).
"""

from __future__ import annotations

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


@pytest.fixture
def client() -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="192.0.2.10", access_code="fixture", serial_number="Synthetic")
    c.state.connected = True
    return c


def _frame(ams):
    return {"print": {"command": "push_status", "gcode_state": "FINISH", "ams": ams}}


PARTIAL = {"tray_now": "255", "tray_tar": "255", "ams_status": 0}
FULL = {
    "ams": [{"id": "0", "dry_time": 0, "tray": [{"id": "0", "tray_type": "PLA"}]}],
    "ams_exist_bits": "1",
    "tray_exist_bits": "1",
    "tray_now": "255",
}
NO_AMS = {"ams": [], "ams_exist_bits": "0", "tray_exist_bits": "0", "tray_now": "255"}


def _units(client):
    return client.state.raw_data.get("ams") or []


def test_a_partial_first_frame_leaves_no_dict_behind(client):
    client._process_message(_frame(dict(PARTIAL)))

    assert isinstance(_units(client), list)
    assert all(isinstance(unit, dict) for unit in _units(client))


@pytest.mark.parametrize("full", [FULL, NO_AMS], ids=["with-ams", "no-ams"])
def test_the_full_frame_after_it_is_stored(client, full):
    client._process_message(_frame(dict(PARTIAL)))
    client._process_message(_frame({**full, "ams": [dict(u) for u in full["ams"]]}))

    assert _units(client) == [u for u in _units(client) if isinstance(u, dict)]
    assert [str(u["id"]) for u in _units(client)] == [str(u["id"]) for u in full["ams"]]


def test_a_dict_already_stored_does_not_stop_the_next_frame(client):
    """A process that already holds the dict recovers on the next full frame."""
    client.state.raw_data["ams"] = dict(PARTIAL)

    client._handle_ams_data({**FULL, "ams": [dict(u) for u in FULL["ams"]]})

    assert [str(u["id"]) for u in _units(client)] == ["0"]


def test_the_status_effect_key_reads_such_a_state(client):
    from backend.app.main import _printer_status_effect_key

    client._process_message(_frame(dict(PARTIAL)))

    _printer_status_effect_key(client.state)
