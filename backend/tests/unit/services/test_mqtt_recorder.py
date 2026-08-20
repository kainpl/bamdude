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
        self.publish_handlers = []
        self.connect_calls = 0

    def register_raw_message_handler(self, h):
        self.handlers.append(h)

    def unregister_raw_message_handler(self, h):
        self.handlers.remove(h)

    def register_raw_publish_handler(self, h):
        self.publish_handlers.append(h)

    def unregister_raw_publish_handler(self, h):
        self.publish_handlers.remove(h)

    def publish(self, topic, payload):
        for h in list(self.publish_handlers):
            h(topic, json.dumps(payload).encode())

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


class TestBothHalvesOfTheConversation:
    """⚠️ A recording that holds only what the printer said is half a transcript.

    The case that proved it: an external-slot spool assignment where the printer
    replied ``result: "success"`` and then, one message later, sent a delta that
    wiped our cache. Reading it needed BOTH sides — what we asked for and what
    came back — and until now the file carried only the second.

    Outgoing is teed at the paho client, once, because
    ``BambuMQTTClient`` publishes from 59 places and ``send_command`` is only
    one of them; the old in-memory debug buffer hooked that one method and so
    missed every command that mattered here.
    """

    def test_a_command_we_send_lands_in_the_file(self, recorder):
        rec, client = recorder
        path = rec.start(7)

        client.publish("device/X/request", {"print": {"command": "ams_filament_setting"}})

        assert _wait_for(path, lambda t: "ams_filament_setting" in t)

    def test_each_line_says_which_way_it_went(self, recorder):
        rec, client = recorder
        path = rec.start(7)

        client.publish("device/X/request", {"print": {"command": "extrusion_cali_sel"}})
        client.emit("device/X/report", {"print": {"command": "push_status"}})

        assert _wait_for(path, lambda t: "push_status" in t and "extrusion_cali_sel" in t)
        rows = [ln.split("\t") for ln in path.read_text(encoding="utf-8").splitlines() if ln]
        directions = {r[1] for r in rows}
        assert directions == {"out", "in"}, "a transcript that cannot tell the sides apart is not readable"
        sent = next(r for r in rows if "extrusion_cali_sel" in r[3])
        assert sent[1] == "out"

    def test_stopping_detaches_the_outgoing_side_too(self, recorder):
        rec, client = recorder
        rec.start(7)
        rec.stop(7)

        assert client.publish_handlers == [], "a stopped recording kept teeing our commands"


class TestReadingARecordingBack:
    """The dialog reads the file, so the recorder owns parsing it.

    ⚠️ ``file_for`` only knows a path while the recording runs — it is dropped on
    stop. A stopped recording is exactly what somebody wants to read, so lookup
    goes by the naming convention on disk instead.
    """

    def test_a_stopped_recording_is_still_findable(self, recorder):
        rec, client = recorder
        path = rec.start(7)
        client.emit("device/X/report", {"print": {"command": "push_status"}})
        assert _wait_for(path, lambda t: "push_status" in t)
        rec.stop(7)

        assert rec.file_for(7) is None, "the live handle is gone, as before"
        assert rec.paths_for(7) == [path], "but the file on disk is still ours to find"

    def test_another_printers_recording_is_not_ours(self, recorder):
        rec, client = recorder
        rec.start(7)
        (rec.log_dir / "mqtt" / "mqtt-20260101-9.log").write_text("x\n", encoding="utf-8")

        assert all(p.name.endswith("-7.log") for p in rec.paths_for(7))

    def test_tail_parses_the_columns(self, recorder):
        rec, client = recorder
        path = rec.start(7)
        client.publish("device/X/request", {"print": {"command": "ams_filament_setting"}})
        client.emit("device/X/report", {"print": {"command": "push_status"}})
        assert _wait_for(path, lambda t: "push_status" in t)

        entries = rec.tail(7, limit=10)

        assert [e["direction"] for e in entries] == ["out", "in"]
        assert entries[0]["topic"] == "device/X/request"
        assert entries[0]["payload"]["print"]["command"] == "ams_filament_setting"
        assert entries[0]["timestamp"]

    def test_tail_returns_the_end_not_the_beginning(self, recorder):
        """⚠️ Nothing caps the file. Reading it whole to show the last screenful
        is how a debugging aid becomes the thing that falls over."""
        rec, client = recorder
        path = rec.start(7)
        for i in range(50):
            client.emit("device/X/report", {"print": {"seq": i}})
        assert _wait_for(path, lambda t: t.count("\n") >= 50)

        entries = rec.tail(7, limit=5)

        assert len(entries) == 5
        assert [e["payload"]["print"]["seq"] for e in entries] == [45, 46, 47, 48, 49]

    def test_a_line_that_is_not_json_still_comes_back(self, recorder):
        """A truncated last line, or a payload the printer sent as plain text,
        must not blank the whole view."""
        rec, _ = recorder
        path = rec.start(7)
        path.write_text("2026-08-21T00:00:00+00:00\tin\tdevice/X/report\tnot json\n", encoding="utf-8")

        entries = rec.tail(7, limit=10)

        assert entries[0]["payload"] == "not json"

    def test_nothing_recorded_yet_is_an_empty_list(self, recorder):
        rec, _ = recorder
        assert rec.tail(7, limit=10) == []
        assert rec.paths_for(7) == []

    def test_delete_removes_the_files_and_reports_how_many(self, recorder):
        rec, client = recorder
        path = rec.start(7)
        client.emit("device/X/report", {"print": {"a": 1}})
        assert _wait_for(path, lambda t: '"a"' in t)
        rec.stop(7)

        assert rec.delete(7) == 1
        assert rec.paths_for(7) == []

    def test_deleting_while_recording_keeps_recording(self, recorder):
        """⚠️ Clear means "start the transcript over", not "stop watching" — the
        badge stays on, so stopping here would leave it lying."""
        rec, client = recorder
        path = rec.start(7)
        client.emit("device/X/report", {"print": {"a": 1}})
        assert _wait_for(path, lambda t: '"a"' in t)

        rec.delete(7)

        assert rec.is_recording(7) is True
        client.emit("device/X/report", {"print": {"b": 2}})
        assert _wait_for(rec.file_for(7), lambda t: '"b"' in t)
