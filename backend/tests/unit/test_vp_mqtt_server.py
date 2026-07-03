"""Tests for Virtual Printer MQTT server."""

import ast
import asyncio
import inspect
import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.services.virtual_printer.mqtt_server import SimpleMQTTServer


def _build_connect_payload(
    keep_alive: int,
    access_code: str = "deadbeef",
    username: str = "bblp",
    client_id: str = "orca",
) -> bytes:
    """Build an MQTT CONNECT variable-header + payload (without the fixed header).

    Layout matches the parser in ``_handle_connect``:
    proto_name_len(2) + "MQTT"(4) + level(1) + flags(1) + keepalive(2) +
    client_id_len(2) + client_id + username_len(2) + username +
    password_len(2) + password.
    """
    proto = b"MQTT"
    parts = bytearray()
    parts += len(proto).to_bytes(2, "big") + proto
    parts += bytes([0x04, 0xC2])  # protocol level 4 (MQTT 3.1.1), flags: user+pass+clean
    parts += keep_alive.to_bytes(2, "big")
    cid = client_id.encode("utf-8")
    parts += len(cid).to_bytes(2, "big") + cid
    user = username.encode("utf-8")
    parts += len(user).to_bytes(2, "big") + user
    pw = access_code.encode("utf-8")
    parts += len(pw).to_bytes(2, "big") + pw
    return bytes(parts)


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


class TestAuthRateLimit:
    """Per-IP MQTT brute-force guard (upstream Bambuddy v0.2.4.5)."""

    def test_under_limit_not_rate_limited(self):
        server = _make_server()
        for _ in range(4):  # one below the cap
            server._record_auth_failure("1.2.3.4")
        assert server._is_auth_rate_limited("1.2.3.4") is False

    def test_at_limit_is_rate_limited(self):
        from backend.app.services.virtual_printer.mqtt_server import _AUTH_RATE_LIMIT_MAX_ATTEMPTS

        server = _make_server()
        for _ in range(_AUTH_RATE_LIMIT_MAX_ATTEMPTS):
            server._record_auth_failure("1.2.3.4")
        assert server._is_auth_rate_limited("1.2.3.4") is True

    def test_window_recovery_prunes_stale_failures(self):
        import time

        from backend.app.services.virtual_printer.mqtt_server import (
            _AUTH_RATE_LIMIT_MAX_ATTEMPTS,
            _AUTH_RATE_LIMIT_WINDOW_SECONDS,
        )

        server = _make_server()
        # All failures older than the window → pruned, no longer limited.
        old = time.monotonic() - (_AUTH_RATE_LIMIT_WINDOW_SECONDS + 5)
        server._auth_failures["1.2.3.4"] = [old] * _AUTH_RATE_LIMIT_MAX_ATTEMPTS
        assert server._is_auth_rate_limited("1.2.3.4") is False
        assert "1.2.3.4" not in server._auth_failures  # dict stays bounded

    def test_failures_are_per_ip(self):
        from backend.app.services.virtual_printer.mqtt_server import _AUTH_RATE_LIMIT_MAX_ATTEMPTS

        server = _make_server()
        for _ in range(_AUTH_RATE_LIMIT_MAX_ATTEMPTS):
            server._record_auth_failure("1.1.1.1")
        assert server._is_auth_rate_limited("1.1.1.1") is True
        assert server._is_auth_rate_limited("2.2.2.2") is False

    def test_success_clears_failure_history(self):
        from backend.app.services.virtual_printer.mqtt_server import _AUTH_RATE_LIMIT_MAX_ATTEMPTS

        server = _make_server()
        for _ in range(_AUTH_RATE_LIMIT_MAX_ATTEMPTS):
            server._record_auth_failure("1.2.3.4")
        server._clear_auth_failures("1.2.3.4")
        assert server._is_auth_rate_limited("1.2.3.4") is False


class TestPendingRequestRouting:
    """Per-slicer response routing (upstream Bambuddy v0.2.4.5)."""

    def test_record_captures_seq_from_nested_block(self):
        server = _make_server()
        server._record_pending_request({"print": {"sequence_id": "42", "command": "x"}}, "clientA")
        assert server._pending_requests == {"42": "clientA"}

    def test_lookup_routes_to_originating_client_and_pops(self):
        import json

        server = _make_server()
        server._record_pending_request({"info": {"sequence_id": "7"}}, "clientA")
        payload = json.dumps({"info": {"sequence_id": "7", "result": "ok"}}).encode()
        assert server._lookup_pending_request_client(payload) == "clientA"
        # One-shot: the entry is popped so a later push falls through to broadcast.
        assert server._lookup_pending_request_client(payload) is None

    def test_fifo_eviction_at_cap(self):
        from backend.app.services.virtual_printer.mqtt_server import _PENDING_REQUEST_MAX_ENTRIES

        server = _make_server()
        for i in range(_PENDING_REQUEST_MAX_ENTRIES + 10):
            server._record_pending_request({"print": {"sequence_id": str(i)}}, f"c{i}")
        assert len(server._pending_requests) == _PENDING_REQUEST_MAX_ENTRIES
        # The oldest keys were evicted; the newest survive.
        assert "0" not in server._pending_requests
        assert str(_PENDING_REQUEST_MAX_ENTRIES + 9) in server._pending_requests

    def test_malformed_payload_falls_back_to_broadcast(self):
        server = _make_server()
        assert server._lookup_pending_request_client(b"not json{{{") is None

    def test_unrecorded_seq_falls_back_to_broadcast(self):
        import json

        server = _make_server()
        payload = json.dumps({"print": {"sequence_id": "999"}}).encode()
        assert server._lookup_pending_request_client(payload) is None


class TestHandleClientIdleConnection:
    """``_handle_client`` must NOT close idle authenticated clients on a
    keepalive boundary (#1548 round 2).

    Round 1 shipped the keepalive parser + 1.5× read timeout per MQTT spec
    §4.4. The reporter then confirmed that the same OrcaSlicer install which
    stays connected to a real Bambu P1S indefinitely was being disconnected
    at exactly ``keep_alive × 1.5`` — pcap showed Orca sends zero MQTT packets
    after the initial burst (no PINGREQ at all). Real Bambu firmware does not
    enforce §4.4; we now match that and rely on TCP keepalive (SO_KEEPALIVE)
    for dead-connection detection.
    """

    @pytest.mark.asyncio
    async def test_idle_client_stays_open_past_one_and_a_half_times_keepalive(self):
        """Round-2 regression guard: a client negotiates keepalive=2 and then
        sits idle. Round 1 would have closed at ~3 s (1.5×). Now the handler
        must still be running well past that boundary — the only thing that
        ends the loop is a DISCONNECT, peer close, or server shutdown."""
        server = _make_server()
        server._running = True

        reader = asyncio.StreamReader()
        connect_payload = _build_connect_payload(keep_alive=2)
        rl = len(connect_payload)
        assert rl < 128
        reader.feed_data(bytes([0x10, rl]) + connect_payload)

        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        writer.get_extra_info = MagicMock(side_effect=lambda name: ("1.2.3.4", 12345) if name == "peername" else None)
        server._send_status_report = AsyncMock()

        task = asyncio.create_task(server._handle_client(reader, writer))

        # 4 s is well past round-1's 3 s (1.5×2) timeout and any conceivable
        # async-scheduler drift.
        await asyncio.sleep(4.0)

        assert not task.done(), "handler must still be waiting on idle reader"
        assert not writer.close.called, "connection must not be closed by keepalive timeout"

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_so_keepalive_set_on_socket_after_connect(self):
        """The application-level read timeout was removed; TCP keepalive
        replaces it for dead-connection detection. Verify the handler sets
        SO_KEEPALIVE on the underlying socket the moment auth succeeds."""
        server = _make_server()
        server._running = True

        reader = asyncio.StreamReader()
        connect_payload = _build_connect_payload(keep_alive=60)
        rl = len(connect_payload)
        assert rl < 128
        reader.feed_data(bytes([0x10, rl]) + connect_payload)

        sock = MagicMock()
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        def _get_extra_info(name):
            if name == "socket":
                return sock
            if name == "peername":
                return ("1.2.3.4", 12345)
            return None

        writer.get_extra_info = MagicMock(side_effect=_get_extra_info)
        server._send_status_report = AsyncMock()

        task = asyncio.create_task(server._handle_client(reader, writer))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        sock.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
