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
