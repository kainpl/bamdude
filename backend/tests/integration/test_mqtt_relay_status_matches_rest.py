"""The relay's six status fields read as the REST status does — a drying cycle included (N06 follow-up).

The relay shapes its status with ``printer_state_to_dict`` (upstream #3401). Upstream calls it
without the AMS drying targets, so during a cycle BamDude started the relay's AMS unit showed no
filament or target temperature while the card and the REST status did. Ours passes the targets every
other caller passes (owner 2026-11-27: worth departing from upstream for). ``ams`` stays ``null`` on a
printer without an AMS — the WebSocket's shape, as upstream.
"""

from unittest.mock import MagicMock

import pytest

from backend.app.api.routes import printers
from backend.app.services.bambu_mqtt import HMSError
from backend.app.services.mqtt_relay import _STATUS_FIELDS_FROM_STATE, MQTTRelayService
from backend.tests.integration.test_status_shapers_agree import _ams_state

DRYING = {0: {"filament": "PETG", "temp": 65}}


def _state():
    state = _ams_state()
    state.state = "RUNNING"
    state.door_open = True
    state.active_extruder = 1
    state.tray_now = 2
    state.hms_errors = [HMSError(code="0x0700_8010", attr=117473280, module=7, severity=2)]
    return state


async def _rest_and_relay(printer_factory, db_session, monkeypatch, state, drying):
    printer = await printer_factory(name="Relay", model="X2D")
    monkeypatch.setattr(printers.printer_manager, "get_status", lambda _id: state)
    monkeypatch.setattr(printers.printer_manager, "get_drying_targets", lambda _id: drying)
    rest = (await printers._build_printer_status(printer, db_session)).model_dump()
    relay = MQTTRelayService()
    relay.enabled = relay.connected = True
    relay._publish = MagicMock()
    await relay.on_printer_status(printer.id, state, "Relay", "SN-RELAY")
    return rest, relay._publish.call_args.args[1]


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("drying", [None, DRYING], ids=["no-cycle", "bamdude-drying-cycle"])
async def test_the_six_fields_equal_the_rest_status(printer_factory, db_session, monkeypatch, drying):
    rest, payload = await _rest_and_relay(printer_factory, db_session, monkeypatch, _state(), drying)

    assert {field: payload[field] for field in _STATUS_FIELDS_FROM_STATE} == {
        field: rest[field] for field in _STATUS_FIELDS_FROM_STATE
    }
    if drying:
        assert (payload["ams"][0]["dry_filament"], payload["ams"][0]["dry_target_temp"]) == ("PETG", 65)
