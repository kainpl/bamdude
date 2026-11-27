"""Retained relay state must reflect the canonical plate gate and live fans."""

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.mqtt_relay import MQTTRelayService


@pytest.mark.asyncio
async def test_plate_clear_edge_updates_dedicated_topic_and_retained_status():
    relay = MQTTRelayService()
    relay.enabled = relay.connected = True
    relay._publish = MagicMock()
    # A real PrinterState: the status payload is shaped by printer_state_to_dict too (upstream #3401).
    state = PrinterState(connected=True, state="IDLE", airduct_parts={10: {"state": 63}, 3: {"state": 1}})

    await relay.on_printer_status(1, state, "P1", "SN1")
    first = relay._publish.call_args.args[1]
    assert first["awaiting_plate_clear"] is False
    assert first["left_aux_fan_speed"] == 63
    assert first["exhaust_fan_present"] is True

    await relay.on_plate_clear_state(1, "P1", "SN1", True)
    calls = relay._publish.call_args_list
    assert calls[-2].args[0] == "bamdude/printers/SN1/plate_clear"
    assert calls[-2].args[1]["awaiting"] is True
    assert calls[-2].kwargs["retain"] is True
    assert calls[-1].args[0] == "bamdude/printers/SN1/status"
    assert calls[-1].args[1]["awaiting_plate_clear"] is True
    assert calls[-1].kwargs["retain"] is True

    await relay.on_plate_clear_state(1, "P1", "SN1", False)
    assert relay._publish.call_args.args[1]["awaiting_plate_clear"] is False
