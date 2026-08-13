"""The layer-change callback reports the edge, not just the new value.

A "fire at layer N" trigger has to ask whether the print *crossed* N, and only
``bambu_mqtt`` knows what the layer was a moment ago. Keeping that number
anywhere else would be a second source of truth for it.
"""

from unittest.mock import MagicMock

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _client(on_layer_change) -> BambuMQTTClient:
    c = BambuMQTTClient(
        ip_address="1.2.3.4",
        serial_number="P1S001",
        access_code="12345678",
        model="P1S",
        on_layer_change=on_layer_change,
    )
    c._client = MagicMock()
    c.state.connected = True
    return c


def test_the_callback_gets_both_layers() -> None:
    seen = []
    c = _client(lambda new, prev: seen.append((new, prev)))

    c._update_state({"layer_num": 4})
    c._update_state({"layer_num": 5})

    assert seen == [(4, 0), (5, 4)]


def test_a_skipped_report_still_shows_the_edge_it_jumped() -> None:
    """MQTT reports do get dropped; 48 -> 52 has still passed 50."""
    seen = []
    c = _client(lambda new, prev: seen.append((new, prev)))

    c._update_state({"layer_num": 48})
    c._update_state({"layer_num": 52})

    assert seen[-1] == (52, 48)


def test_a_decrease_fires_nothing() -> None:
    """Firmware resets layer_num to 0 on cancel."""
    seen = []
    c = _client(lambda new, prev: seen.append((new, prev)))

    c._update_state({"layer_num": 7})
    c._update_state({"layer_num": 0})

    assert seen == [(7, 0)]
