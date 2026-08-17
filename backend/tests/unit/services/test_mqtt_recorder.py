"""Server-side MQTT recording.

Replaces "open a terminal, run the sniffer, and do not close the window". The
recorder tees the connection BamDude already holds — it must never open one of
its own, which is the mistake that made a support bundle disturb every printer
on the farm.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from backend.app.services.mqtt_recorder import MqttRecorder

pytestmark = pytest.mark.unit


class _Client:
    """Stands in for BambuMQTTClient's raw fan-out."""

    def __init__(self):
        self.handlers = []
        self.connect_calls = 0

    def register_raw_message_handler(self, h):
        self.handlers.append(h)

    def unregister_raw_message_handler(self, h):
        self.handlers.remove(h)

    def connect(self, *a, **kw):
        self.connect_calls += 1

    def emit(self, topic, payload):
        for h in list(self.handlers):
            h(topic, json.dumps(payload).encode())


@pytest.fixture
def recorder(tmp_path):
    client = _Client()
    manager = MagicMock()
    manager.get_client.return_value = client
    return MqttRecorder(log_dir=tmp_path, printer_manager=manager), client


def _wait_for(path, predicate, timeout=3.0):
    """Wait for a condition rather than sleeping blindly — the writer is a thread."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and predicate(path.read_text(encoding="utf-8")):
            return True
        time.sleep(0.02)
    return False


def test_it_records_what_the_existing_client_receives(recorder):
    r, client = recorder
    path = r.start(3)
    client.emit("device/01P00A/report", {"print": {"gcode_state": "RUNNING"}})

    assert _wait_for(path, lambda t: "RUNNING" in t)
    r.stop(3)


def test_it_never_opens_its_own_connection(recorder):
    """⚠️ The whole point. A second MQTT session to a connected printer is what
    the diagnostic fix removed, and this must not reintroduce it."""
    r, client = recorder
    r.start(3)
    client.emit("device/01P00A/report", {"a": 1})
    r.stop(3)

    assert client.connect_calls == 0


def test_the_raw_handler_does_not_touch_the_disk(recorder):
    """paho's thread only enqueues. Proven by holding the writer: the emit still
    returns immediately, and the line lands once the writer is released."""
    r, client = recorder
    path = r.start(3)
    r._writer_paused.set()

    started = time.time()
    client.emit("device/01P00A/report", {"a": 1})
    elapsed = time.time() - started

    r._writer_paused.clear()
    assert elapsed < 0.05, "the raw handler blocked on I/O"
    assert _wait_for(path, lambda t: '"a"' in t)
    r.stop(3)


def test_a_full_queue_drops_rather_than_blocking(recorder):
    """⚠️ A recorder must never become backpressure on the client that feeds
    every other feature, so a stalled writer costs messages, not ingest."""
    r, client = recorder
    r.start(3)
    r._writer_paused.set()

    started = time.time()
    for _ in range(200):
        client.emit("device/01P00A/report", {"a": 1})
    elapsed = time.time() - started

    r._writer_paused.clear()
    assert elapsed < 1.0, "emitting blocked once the writer stopped draining"
    r.stop(3)


def test_stopping_detaches_the_handler(recorder):
    r, client = recorder
    r.start(3)
    r.stop(3)

    assert client.handlers == []
    assert r.is_recording(3) is False


def test_starting_twice_is_the_same_recording(recorder):
    """The UI can send the same intent twice; a second handler would write every
    line twice."""
    r, client = recorder
    first = r.start(3)
    second = r.start(3)

    assert first == second
    assert len(client.handlers) == 1
    r.stop(3)


def test_a_printer_with_no_client_is_refused_loudly(recorder):
    """Recording cannot start before there is a session to tee. Silence here
    would look like a recording that is running and is not."""
    r, _ = recorder
    r._manager.get_client.return_value = None

    with pytest.raises(RuntimeError, match="no live MQTT client"):
        r.start(99)


def test_the_file_is_named_by_date_and_printer(recorder):
    r, _ = recorder
    path = r.start(7)

    assert path.name.startswith("mqtt-")
    assert path.name.endswith("-7.log")
    assert path.parent.name == "mqtt"
    r.stop(7)


def test_size_is_reported_for_the_card(recorder):
    """The card shows this beside the badge. Nothing caps the file, so this
    number is what makes a forgotten recording visible."""
    r, client = recorder
    path = r.start(3)
    client.emit("device/01P00A/report", {"padding": "x" * 500})
    _wait_for(path, lambda t: "padding" in t)

    assert r.size_bytes(3) > 0
    r.stop(3)
    assert r.size_bytes(3) == 0


def test_a_rebuilt_session_is_re_attached_not_abandoned(recorder):
    """⚠️ The trap this codebase keeps falling into.

    ``connection_watchdog`` rebuilds a stalled MQTT session by creating a NEW
    client, and the handler registered on the old one dies with it. A recording
    asked to run "until stopped" would stop at the first reconnect — silently,
    with the badge still showing and the file simply never growing again.
    """
    r, first_client = recorder
    path = r.start(3)

    replacement = _Client()
    r._manager.get_client.return_value = replacement
    r.start(3)  # what resume_recordings does on every sweep

    assert len(replacement.handlers) == 1, "not attached to the live client"
    replacement.emit("device/01P00A/report", {"after": "rebuild"})
    assert _wait_for(path, lambda t: "rebuild" in t)
    assert r.file_for(3) == path, "a rebuilt session must not shred the recording into a second file"
    r.stop(3)


def test_re_attaching_does_not_double_register(recorder):
    """Two handlers on one client would write every line twice."""
    r, client = recorder
    r.start(3)
    r.start(3)

    assert len(client.handlers) == 1
    r.stop(3)
