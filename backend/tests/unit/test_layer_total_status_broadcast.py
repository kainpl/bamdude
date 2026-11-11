"""A late MQTT total must reach the card without another layer or temperature change."""

from unittest.mock import AsyncMock, MagicMock

from backend.app import main
from backend.app.services.bambu_mqtt import BambuMQTTClient


async def test_total_only_update_and_clear_broadcast_but_duplicate_does_not(monkeypatch):
    for name in (
        "_last_status_broadcast",
        "_last_printer_state",
        "_last_printer_state_generation",
        "_printer_last_connected",
    ):
        monkeypatch.setattr(main, name, {})
    manager = MagicMock()
    manager.session_still_lives.return_value = True
    manager.get_printer.return_value = None
    manager.get_model.return_value = "A1 Mini"
    manager.get_drying_targets.return_value = {}
    monkeypatch.setattr(main, "printer_manager", manager)
    monkeypatch.setattr(main, "_repeat_available_for", AsyncMock(return_value=False))
    send = AsyncMock()
    monkeypatch.setattr(main.ws_manager, "send_printer_status", send)

    client = BambuMQTTClient(ip_address="127.0.0.1", serial_number="OFFLINE-LAYERS", access_code="synthetic")
    client._process_message(
        {"print": {"gcode_state": "RUNNING", "gcode_file": "part.gcode", "layer_num": 29, "mc_percent": 10}}
    )
    client.state.connected = True
    await main.on_printer_status_change(1, client.state)
    client._process_message({"print": {"total_layer_num": 693}})
    await main.on_printer_status_change(1, client.state)
    await main.on_printer_status_change(1, client.state)
    assert [(c.args[1]["layer_num"], c.args[1]["total_layers"]) for c in send.await_args_list] == [
        (29, 0),
        (29, 693),
    ]

    # A lifecycle reset is meaningful too, even with all other display fields held constant.
    client.state.total_layers = 0
    await main.on_printer_status_change(1, client.state)
    assert send.await_count == 3
    assert send.await_args.args[1]["total_layers"] == 0
