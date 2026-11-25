"""A printer command is never sent again on a later connection.

paho re-sends every QoS 1 publish still waiting for its PUBACK when the same
client reconnects, and Bambu's broker often never sends one. These tests drive a
REAL paho client through its own ``reconnect()`` and CONNACK handling with only
the socket stubbed, so a paho upgrade that changes the order the guarantee rests
on fails here instead of bringing the replay back.
"""

import json
import time
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt
import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.utils.paho_teardown import retire_paho_client

STOP = json.dumps({"print": {"command": "stop", "sequence_id": "1"}})
PROJECT_FILE = json.dumps({"print": {"command": "project_file", "sequence_id": "2"}})
TOPIC = "device/SNREPLAY/request"


def _paho() -> mqtt.Client:
    return mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="bamdude_test_replay",
        protocol=mqtt.MQTTv311,
    )


def _queue_unacked(client: mqtt.Client, topic: str, *payloads: str) -> None:
    """Commands sent on the old connection whose PUBACK never came."""
    for payload in payloads:
        client.publish(topic, payload, qos=1)  # no socket: stays in the outbox
    for message in client._out_messages.values():
        message.state = mqtt.mqtt_ms_wait_for_puback
        client._inflight_messages += 1


def _reconnect(client: mqtt.Client) -> list[str]:
    """paho's own reconnect of the same client, then its CONNACK handling;
    returns the payloads put on the wire."""
    sent: list[str] = []

    def send_publish(mid, topic, payload=b"", *args, **kwargs):
        sent.append(payload.decode() if isinstance(payload, bytes) else payload)
        return mqtt.MQTTErrorCode.MQTT_ERR_SUCCESS

    sock = MagicMock()
    sock.send.side_effect = len
    sock.recv.side_effect = [bytes([mqtt.CONNACK]), b"\x02", b"\x00\x00"]
    if not client._host:
        client.connect_async("192.0.2.1", 8883)
    with (
        patch.object(client, "_create_socket", return_value=sock),
        patch.object(client, "_send_connect", return_value=mqtt.MQTTErrorCode.MQTT_ERR_SUCCESS),
        patch.object(client, "_send_publish", side_effect=send_publish),
    ):
        assert client.reconnect() == mqtt.MQTTErrorCode.MQTT_ERR_SUCCESS
        assert client._packet_read() == mqtt.MQTTErrorCode.MQTT_ERR_SUCCESS
    return sent


@pytest.fixture
def printer():
    printer = BambuMQTTClient(
        ip_address="192.168.1.100", serial_number="SNREPLAY", access_code="12345678", model="A1 mini"
    )
    paho = _paho()
    paho.on_connect = printer._on_connect
    printer._client = paho
    return printer


def test_paho_re_sends_the_old_outbox_only_after_on_connect():
    # paho's own behaviour, which our drain in on_connect relies on.
    client = _paho()
    seen: list[tuple[int, int]] = []
    client.on_connect = lambda c, *a: seen.append((len(c._out_messages), c._inflight_messages))
    _queue_unacked(client, TOPIC, STOP, PROJECT_FILE)

    sent = _reconnect(client)

    assert seen == [(2, 0)]
    assert sent == [STOP, PROJECT_FILE]


def test_a_stale_stop_and_project_file_are_not_sent_again(printer):
    _queue_unacked(printer._client, printer.topic_publish, STOP, PROJECT_FILE)

    sent = _reconnect(printer._client)

    assert STOP not in sent and PROJECT_FILE not in sent
    assert printer.state.connected is True


def test_a_virtual_printer_bridge_command_is_dropped_too(printer):
    printer.publish_raw(printer.topic_publish, PROJECT_FILE)
    for message in printer._client._out_messages.values():
        message.state = mqtt.mqtt_ms_wait_for_puback

    assert PROJECT_FILE not in _reconnect(printer._client)


def test_a_retired_client_keeps_no_outbox_to_replay():
    # A client let go while stuck in its handshake can still finish it and
    # connect; its on_connect is gone, so its outbox must be gone too.
    client = _paho()
    _queue_unacked(client, TOPIC, STOP)
    with patch.object(client, "loop_stop"), patch.object(client, "disconnect"):
        thread = retire_paho_client(client, "SNREPLAY")
        if thread is not None:
            thread.join(timeout=5)
        for _ in range(100):
            if not client._out_messages:
                break
            time.sleep(0.01)

    assert STOP not in _reconnect(client)
