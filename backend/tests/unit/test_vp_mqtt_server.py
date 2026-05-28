"""Tests for Virtual Printer MQTT server."""

import ast
import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.app.services.virtual_printer.mqtt_server import SimpleMQTTServer


def _make_server(serial: str = "01P00A391800001") -> SimpleMQTTServer:
    """Build a SimpleMQTTServer with dummy cert paths (start() is never called)."""
    return SimpleMQTTServer(
        serial=serial,
        access_code="deadbeef",
        cert_path=Path("/tmp/unused.crt"),  # nosec B108
        key_path=Path("/tmp/unused.key"),  # nosec B108
        model="C12",
    )


class TestMQTTServerNoGlobalState:
    """Ensure MQTT server doesn't set global asyncio state."""

    def test_no_global_exception_handler(self):
        """MQTT server must not call set_exception_handler().

        set_exception_handler() is global to the event loop. When multiple
        VP instances run, each would overwrite the previous handler,
        causing lost error context and spurious 'Unhandled exception in
        client_connected_cb' messages.
        """
        source = inspect.getsource(SimpleMQTTServer)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "set_exception_handler":
                raise AssertionError(
                    "SimpleMQTTServer must not call set_exception_handler(). "
                    "It overwrites the global asyncio exception handler, "
                    "breaking multi-VP setups."
                )


class TestHandlePublishNullTerminatorTolerance:
    """Regression for #927 — OrcaSlicer Linux appends \\x00 to MQTT payloads."""

    def test_handle_publish_tolerates_null_terminated_payload(self):
        """The handler must parse and respond rather than silently dropping.

        Real bytes captured from a #927 support log: the JSON ends with an
        extra \\x00 that strict json.loads rejects. Before this fix, every
        pushall/get_version/project_file from OrcaSlicer on Linux was
        discarded with no log line.
        """
        server = _make_server(serial="01P00A391800001")
        server._client_serials["c1"] = server.serial

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        topic = "device/01P00A391800001/request"
        topic_bytes = topic.encode("utf-8")
        message_bytes = b'{"pushing":{"command":"pushall","sequence_id":"7"}}\x00'
        payload = len(topic_bytes).to_bytes(2, "big") + topic_bytes + message_bytes

        asyncio.run(server._handle_publish(0x30, payload, writer, "c1"))

        all_bytes = b"".join(call.args[0] for call in writer.write.call_args_list)
        assert b"device/01P00A391800001/report" in all_bytes
        assert b'"command": "push_status"' in all_bytes


class TestStalePrepareReporting:
    """A ``PREPARE`` left by a ``project_file`` whose upload never completed
    must not advertise the VP as busy forever. BambuStudio and OrcaSlicer both
    map gcode_state → print_status and treat PREPARE as in-printing, so a stale
    PREPARE makes every pre-flight read "busy with another print job"."""

    def test_reported_state_downgrades_stale_prepare_to_idle(self):
        server = _make_server()
        server._gcode_state = "PREPARE"
        assert server._active_uploads == 0
        assert server._reported_gcode_state() == "IDLE"

    def test_reported_state_keeps_prepare_during_active_upload(self):
        server = _make_server()
        server._gcode_state = "PREPARE"
        server.upload_started()
        assert server._reported_gcode_state() == "PREPARE"
        # Once the upload ends (success or failure) the leftover PREPARE is stale.
        server.upload_finished()
        assert server._reported_gcode_state() == "IDLE"

    def test_upload_counter_never_goes_negative(self):
        server = _make_server()
        server.upload_finished()
        assert server._active_uploads == 0
        # And two concurrent uploads need both ends before we report idle.
        server.upload_started()
        server.upload_started()
        server._gcode_state = "PREPARE"
        server.upload_finished()
        assert server._reported_gcode_state() == "PREPARE"
        server.upload_finished()
        assert server._reported_gcode_state() == "IDLE"

    def test_non_prepare_states_pass_through(self):
        server = _make_server()
        for st in ("IDLE", "FINISH", "RUNNING", "FAILED"):
            server._gcode_state = st
            assert server._reported_gcode_state() == st

    def test_resolve_stale_prepare_clears_to_idle(self):
        server = _make_server()
        server._gcode_state = "PREPARE"
        server._current_file = "x.3mf"
        server._prepare_percent = "0"
        server.resolve_stale_prepare()
        assert server._gcode_state == "IDLE"
        assert server._current_file == ""
        # Non-PREPARE states are left untouched.
        server._gcode_state = "FINISH"
        server.resolve_stale_prepare()
        assert server._gcode_state == "FINISH"

    def test_status_push_advertises_idle_for_stale_prepare(self):
        """End-to-end: the actual push_status bytes carry IDLE, not PREPARE."""
        server = _make_server()
        server._gcode_state = "PREPARE"  # no upload in flight

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        asyncio.run(server._send_status_report(writer, serial=server.serial))

        pushed = b"".join(call.args[0] for call in writer.write.call_args_list)
        assert b'"gcode_state": "IDLE"' in pushed
        assert b'"gcode_state": "PREPARE"' not in pushed

    def test_recent_prepare_within_grace_reports_prepare(self):
        """The window between project_file and the FTP STOR: PREPARE is live."""
        import time

        server = _make_server()
        server._gcode_state = "PREPARE"
        server._prepare_set_monotonic = time.monotonic()  # just now, no upload yet
        assert server._active_uploads == 0
        assert server._reported_gcode_state() == "PREPARE"

    def test_prepare_past_grace_reports_idle(self):
        """A project_file with no upload after the grace elapses is stale → IDLE."""
        import time

        from backend.app.services.virtual_printer.mqtt_server import PREPARE_GRACE_SECONDS

        server = _make_server()
        server._gcode_state = "PREPARE"
        server._prepare_set_monotonic = time.monotonic() - (PREPARE_GRACE_SECONDS + 60)
        assert server._reported_gcode_state() == "IDLE"

    def test_project_file_response_stamps_prepare_and_reports_prepare(self):
        """``project_file`` sets PREPARE and stamps the grace clock, so the gap
        before the upload starts reports PREPARE rather than a premature IDLE."""
        server = _make_server()
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        asyncio.run(server._send_print_response(writer, "1", "foo.3mf"))

        assert server._gcode_state == "PREPARE"
        assert server._active_uploads == 0
        assert server._reported_gcode_state() == "PREPARE"  # within grace
