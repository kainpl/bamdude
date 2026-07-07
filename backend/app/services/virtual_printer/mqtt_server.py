"""MQTT broker for virtual printer.

Implements an MQTT broker that accepts connections from slicers,
authenticates with the configured access code, and logs print commands.
"""

import asyncio
import hmac
import json
import logging
import socket
import ssl
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.app.services.virtual_printer.mqtt_bridge import MQTTBridge

logger = logging.getLogger(__name__)

# Default MQTT port for Bambu printers (MQTT over TLS)
MQTT_PORT = 8883

# Per-IP brute-force guard on the slicer-facing MQTT auth (upstream Bambuddy
# v0.2.4.5). The 8-char access code is reachable by anyone who can hit the VP's
# bind IP (LAN / Tailscale / tunnel); without a limit it is brute-forceable.
# Sliding window, auto-recovers, cleared on a successful auth. Module-level so
# ops can tune them.
_AUTH_RATE_LIMIT_MAX_ATTEMPTS = 5
_AUTH_RATE_LIMIT_WINDOW_SECONDS = 60.0

# Cap on the sequence_id → client_id map used to route a bridge-forwarded
# response back to only the slicer that issued the request (upstream Bambuddy
# v0.2.4.5). FIFO-evicted so a slicer that sends commands without consuming
# responses can't leak memory.
_PENDING_REQUEST_MAX_ENTRIES = 256

# Grace window after a ``project_file`` command during which a ``PREPARE`` with
# no upload yet in flight is still considered live. Covers the gap between the
# slicer's MQTT print command and it opening the FTP data connection (TLS
# handshake + login + PASV + STOR — sub-second normally, a few seconds on slow
# links). Past this with no upload started, the PREPARE is treated as stale and
# reported as IDLE so a cancelled send can't read as "busy" indefinitely.
PREPARE_GRACE_SECONDS = 15.0

# Model code → product_name for version response (must match what slicer expects)
MODEL_PRODUCT_NAMES = {
    "BL-P001": "X1 Carbon",
    "BL-P002": "X1",
    "C13": "X1E",
    "N6": "X2D",
    "N9": "A2L",
    "C11": "P1P",
    "C12": "P1S",
    "N7": "P2S",
    "N2S": "A1",
    "N1": "A1 mini",
    "O1D": "H2D",
    "O1C": "H2C",
    "O1C2": "H2C",
    "O1S": "H2S",
}


class VirtualPrinterMQTTServer:
    """MQTT broker that accepts connections from slicers.

    This is a minimal MQTT broker implementation that:
    - Accepts TLS connections on port 8883
    - Authenticates with username 'bblp' and the configured access code
    - Receives print commands on device/{serial}/request
    - Can publish status on device/{serial}/report
    """

    def __init__(
        self,
        serial: str,
        access_code: str,
        cert_path: Path,
        key_path: Path,
        port: int = MQTT_PORT,
        on_print_command: Callable[[str, dict], None] | None = None,
    ):
        """Initialize the MQTT server.

        Args:
            serial: Virtual printer serial number
            access_code: Password for authentication
            cert_path: Path to TLS certificate
            key_path: Path to TLS private key
            port: Port to listen on (default 8883)
            on_print_command: Callback when print command received (filename, data)
        """
        self.serial = serial
        self.access_code = access_code
        self.cert_path = cert_path
        self.key_path = key_path
        self.port = port
        self.on_print_command = on_print_command
        self._running = False
        self._broker = None
        self._broker_task = None

    async def start(self) -> None:
        """Start the MQTT broker."""
        if self._running:
            return

        # Try to import amqtt
        try:
            from amqtt.broker import Broker
        except ImportError:
            logger.error("amqtt not installed. Run: pip install amqtt")
            return

        logger.info("Starting virtual printer MQTT broker on port %s", self.port)

        # Build broker configuration
        config = {
            "listeners": {
                "default": {
                    "type": "tcp",
                    "bind": f"0.0.0.0:{self.port}",
                    "ssl": "on",
                    "certfile": str(self.cert_path),
                    "keyfile": str(self.key_path),
                },
            },
            "auth": {
                "allow-anonymous": False,
                "plugins": ["auth_custom"],
            },
            "topic-check": {
                "enabled": False,  # Allow any topic
            },
        }

        try:
            self._running = True

            # Create and start broker
            self._broker = Broker(config)

            # Register custom auth plugin
            self._broker.plugins_manager.plugins_handlers["auth_custom"] = self._authenticate

            # Start the broker
            await self._broker.start()
            logger.info("MQTT broker started on port %s", self.port)

            # Keep running
            while self._running:
                await asyncio.sleep(1)

        except OSError as e:
            if e.errno == 98:  # Address already in use
                logger.error("MQTT port %s is already in use", self.port)
            else:
                logger.error("MQTT broker error: %s", e)
        except asyncio.CancelledError:
            logger.debug("MQTT broker task cancelled")
        except Exception as e:
            logger.error("MQTT broker error: %s", e)
        finally:
            await self.stop()

    async def _authenticate(self, session) -> bool:
        """Authenticate MQTT connection.

        Args:
            session: MQTT session with username/password

        Returns:
            True if authentication successful
        """
        username = getattr(session, "username", None)
        password = getattr(session, "password", None)

        # Bambu slicers use 'bblp' as username and access code as password.
        # Constant-time compare closes the auth-code timing side-channel.
        if username == "bblp" and isinstance(password, str) and hmac.compare_digest(password, self.access_code):
            logger.debug("MQTT client authenticated from %s", session.remote_address)
            return True

        logger.warning("MQTT auth failed for user '%s' from %s", username, session.remote_address)
        return False

    async def stop(self) -> None:
        """Stop the MQTT broker."""
        logger.info("Stopping MQTT broker")
        self._running = False

        if self._broker:
            try:
                await self._broker.shutdown()
            except OSError as e:
                logger.debug("Error shutting down MQTT broker: %s", e)
            self._broker = None


class SimpleMQTTServer:
    """Simplified MQTT server using raw sockets.

    This is a fallback implementation that handles basic MQTT protocol
    without requiring the amqtt library. It's less feature-complete but
    more lightweight.
    """

    def __init__(
        self,
        serial: str,
        access_code: str,
        cert_path: Path,
        key_path: Path,
        port: int = MQTT_PORT,
        on_print_command: Callable[[str, dict], None] | None = None,
        model: str = "",
        bind_address: str = "0.0.0.0",  # nosec B104
        vp_name: str = "",
    ):
        self.serial = serial
        self.access_code = access_code
        self.model = model
        self.cert_path = cert_path
        self.key_path = key_path
        self.port = port
        self.on_print_command = on_print_command
        self.bind_address = bind_address
        self.vp_name = vp_name
        self._log_prefix = f"[{vp_name}] " if vp_name else ""
        self._running = False
        self._server = None
        self._clients: dict[str, asyncio.StreamWriter] = {}
        # Per-IP failed-CONNECT timestamps (monotonic) for the brute-force guard.
        # Pruned to the sliding window on each check so the dict stays bounded.
        self._auth_failures: dict[str, list[float]] = {}
        # sequence_id → originating client_id, so a bridge-forwarded response is
        # routed only to the slicer that asked (not fanned out to every slicer).
        # FIFO-evicted at _PENDING_REQUEST_MAX_ENTRIES.
        self._pending_requests: dict[str, str] = {}
        # Set once the listening socket is actually bound so ``is_running`` on
        # the parent instance doesn't report ready before the port is open
        # (V6 readiness barrier, upstream Bambuddy v0.2.4.5).
        self.ready = asyncio.Event()
        # Per-client serial - the serial the slicer actually uses in topics.
        # Populated from SUBSCRIBE/PUBLISH. Lets the VP respond on the topic
        # the slicer is listening on even when it disagrees with self.serial.
        self._client_serials: dict[str, str] = {}
        self._status_push_task: asyncio.Task | None = None
        self._sequence_id = 0

        # Dynamic state for status reports
        self._gcode_state = "IDLE"
        self._current_file = ""
        self._prepare_percent = "0"

        # Number of FTP uploads currently transferring to this VP. Driven by
        # the FTP server through ``upload_started`` / ``upload_finished`` (a
        # counter, not a bool, so two concurrent slicer sessions can't clear
        # each other's in-flight state). ``PREPARE`` is set the moment a slicer
        # sends its ``project_file`` MQTT command, but if the upload that should
        # follow never arrives or the FTP transfer fails, that ``PREPARE`` would
        # otherwise stick forever — and BambuStudio / OrcaSlicer both treat
        # ``PREPARE`` as "in printing", so every later pre-flight reads
        # "The printer is busy with another print job". When no upload is in
        # flight we advertise ``IDLE`` instead of the stale ``PREPARE``.
        self._active_uploads = 0
        # Monotonic timestamp of the last ``project_file`` command. Gives a
        # short grace (``PREPARE_GRACE_SECONDS``) in which a PREPARE with no
        # upload yet is still treated as live — the slicer sends the MQTT print
        # command a moment before it opens the FTP connection.
        self._prepare_set_monotonic = 0.0

        # MQTT bridge for non-proxy modes — set by VirtualPrinterInstance after
        # ``start()``. When the bridge is_active, ``_send_status_report`` serves
        # near-byte-identical real-printer pushes from cache and slicer-issued
        # commands the synthetic flow doesn't handle are forwarded to the real
        # printer. When the target printer goes offline the synthetic fallback
        # resumes automatically.
        self._bridge: MQTTBridge | None = None

    def set_bridge(self, bridge: "MQTTBridge | None") -> None:
        """Attach (or detach) the MQTT bridge that mirrors the target printer."""
        self._bridge = bridge

    async def start(self) -> None:
        """Start the MQTT server."""
        if self._running:
            return

        logger.info("Starting simple MQTT server on port %s", self.port)

        # Create SSL context with Bambu-compatible settings
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(str(self.cert_path), str(self.key_path))
        # Match Bambu printer behavior - accept any client
        ssl_context.verify_mode = ssl.CERT_NONE
        # Allow TLS 1.2 for broader compatibility (some slicers may not support 1.3)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        # Match real Bambu printer cipher behaviour: include the plain-RSA
        # AES-GCM suites the slicer's MQTT-over-TLS ClientHello offers. On
        # hardened-crypto-policy hosts OpenSSL's DEFAULT strips them, leaving no
        # overlap → the handshake fails before any MQTT frame flows (#1610).
        ssl_context.set_ciphers("DEFAULT:AES256-GCM-SHA384:AES128-GCM-SHA256")
        # Disable hostname checking
        ssl_context.check_hostname = False

        # Log certificate info
        import subprocess

        try:
            result = subprocess.run(
                ["openssl", "x509", "-in", str(self.cert_path), "-noout", "-subject", "-issuer"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            logger.info("MQTT SSL cert info: %s", result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass  # Certificate info is for debug logging only; not critical

        logger.info("MQTT SSL context: TLS 1.2+, cert=%s", self.cert_path)

        try:
            self._running = True

            # Wrapper to log ALL connection attempts including SSL errors
            async def connection_handler(reader, writer):
                try:
                    addr = writer.get_extra_info("peername")
                    ssl_obj = writer.get_extra_info("ssl_object")
                    if ssl_obj:
                        logger.info(
                            f"{self._log_prefix}MQTT TLS connection from {addr} - cipher={ssl_obj.cipher()}, version={ssl_obj.version()}"
                        )
                    else:
                        logger.info("%sMQTT connection from %s (no TLS?)", self._log_prefix, addr)
                    await self._handle_client(reader, writer)
                except ssl.SSLError as e:
                    logger.error("MQTT SSL error: %s", e)
                except Exception as e:
                    logger.error("MQTT connection handler error: %s", e)

            self._server = await asyncio.start_server(
                connection_handler,
                self.bind_address,
                self.port,
                ssl=ssl_context,
            )
            self.ready.set()

            logger.info("Simple MQTT server listening on port %s", self.port)

            # Start periodic status push task
            self._status_push_task = asyncio.create_task(self._periodic_status_push())

            async with self._server:
                await self._server.serve_forever()

        except OSError as e:
            if e.errno == 98:  # Address already in use
                logger.error("MQTT port %s is already in use", self.port)
            else:
                logger.error("MQTT server error: %s", e)
        except asyncio.CancelledError:
            logger.debug("MQTT server task cancelled")
        except Exception as e:
            logger.error("MQTT server error: %s", e)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stop the MQTT server."""
        logger.info("Stopping simple MQTT server")
        self._running = False
        self.ready.clear()

        # Stop periodic status push
        if self._status_push_task:
            self._status_push_task.cancel()
            try:
                await self._status_push_task
            except asyncio.CancelledError:
                pass  # Expected when stopping the periodic status push task
            self._status_push_task = None

        # Close all client connections (iterate over copy to avoid modification during iteration)
        for _client_id, writer in list(self._clients.items()):
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass  # Best-effort client connection cleanup; client may have disconnected
        self._clients.clear()
        self._client_serials.clear()

        if self._server:
            try:
                self._server.close()
                await self._server.wait_closed()
            except OSError:
                pass  # Best-effort server shutdown; port may already be released
            self._server = None

    @staticmethod
    def _extract_serial_from_topic(topic: str) -> str | None:
        """Pull the serial out of a `device/{serial}/report|request` topic."""
        if not topic.startswith("device/"):
            return None
        rest = topic[len("device/") :]
        slash = rest.find("/")
        if slash <= 0:
            return None
        return rest[:slash]

    async def _periodic_status_push(self) -> None:
        """Send periodic status updates to all connected clients."""
        logger.info("Starting periodic status push task")
        while self._running:
            try:
                await asyncio.sleep(1)  # Push every 1 second like real printers

                # Send status to all connected clients
                disconnected = []
                for client_id, writer in list(self._clients.items()):
                    try:
                        if writer.is_closing():
                            disconnected.append(client_id)
                            continue
                        serial = self._client_serials.get(client_id, self.serial)
                        await self._send_status_report(writer, serial=serial)
                    except OSError as e:
                        logger.debug("Failed to push status to %s: %s", client_id, e)
                        disconnected.append(client_id)

                # Remove disconnected clients
                for client_id in disconnected:
                    self._clients.pop(client_id, None)
                    self._client_serials.pop(client_id, None)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Periodic status push error: %s", e)

        logger.info("Periodic status push task stopped")

    async def push_raw_to_clients(self, topic: str, payload: bytes) -> None:
        """Publish a pre-serialized MQTT payload on ``topic`` to connected slicers.

        Called by ``MQTTBridge`` from the asyncio loop (scheduled via
        ``run_coroutine_threadsafe`` from paho's network thread).

        Routes the response only to the originating slicer when the payload's
        sequence_id was recorded via ``_record_pending_request`` — otherwise a
        response to slicer A leaked into slicer B's stream on multi-slicer VP
        setups (upstream Bambuddy v0.2.4.5). Falls back to fan-out for
        printer-initiated pushes (push_status etc.) and unrecorded seq ids.
        """
        topic_bytes = topic.encode("utf-8")
        # MQTT remaining-length: 2-byte topic length prefix + topic + message body.
        remaining = 2 + len(topic_bytes) + len(payload)
        packet = bytearray([0x30])  # PUBLISH, QoS 0
        while True:
            byte = remaining % 128
            remaining //= 128
            if remaining > 0:
                byte |= 0x80
            packet.append(byte)
            if remaining == 0:
                break
        packet.extend([len(topic_bytes) >> 8, len(topic_bytes) & 0xFF])
        packet.extend(topic_bytes)
        packet.extend(payload)
        frame = bytes(packet)

        target_client_id = self._lookup_pending_request_client(payload)

        disconnected = []
        for client_id, writer in list(self._clients.items()):
            if target_client_id is not None and client_id != target_client_id:
                continue
            try:
                if writer.is_closing():
                    disconnected.append(client_id)
                    continue
                writer.write(frame)
                try:
                    await asyncio.wait_for(writer.drain(), timeout=5)
                except TimeoutError:
                    logger.debug("MQTT drain timeout pushing bridge frame to %s", client_id)
            except OSError as e:
                logger.debug("Failed to push bridge frame to %s: %s", client_id, e)
                disconnected.append(client_id)

        for client_id in disconnected:
            self._clients.pop(client_id, None)
            self._client_serials.pop(client_id, None)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle an MQTT client connection."""
        addr = writer.get_extra_info("peername")
        client_id = f"{addr[0]}:{addr[1]}" if addr else "unknown"
        logger.info("%sMQTT client connected: %s", self._log_prefix, client_id)

        authenticated = False
        # Per-packet read timeout. 60 s before CONNECT so a silent client can't
        # hold the task forever. After CONNECT we drop the application-level
        # read timeout entirely and rely on TCP keepalive (SO_KEEPALIVE) to
        # detect dead connections — this matches real Bambu firmware, which
        # does not enforce MQTT spec §4.4's 1.5× idle disconnect (#1548 round
        # 2). OrcaSlicer's MQTT client on some platforms emits no PINGREQ at
        # all on idle connections; the same install that stays connected to a
        # real P1S indefinitely was being disconnected from us at keepalive×1.5.
        read_timeout: float | None = 60.0

        try:
            while self._running:
                # Read MQTT fixed header
                try:
                    header = await asyncio.wait_for(reader.read(1), timeout=read_timeout)
                except TimeoutError:
                    break

                if not header:
                    break

                packet_type = (header[0] & 0xF0) >> 4

                # Read remaining length
                remaining_length = await self._read_remaining_length(reader)
                if remaining_length is None:
                    break

                # Read payload
                payload = await reader.read(remaining_length) if remaining_length > 0 else b""

                # Handle packet types
                if packet_type == 1:  # CONNECT
                    source_ip = addr[0] if addr else "unknown"
                    if self._is_auth_rate_limited(source_ip):
                        logger.warning(
                            "%sMQTT auth rate-limited from %s (>=%d failures in %ds)",
                            self._log_prefix,
                            source_ip,
                            _AUTH_RATE_LIMIT_MAX_ATTEMPTS,
                            int(_AUTH_RATE_LIMIT_WINDOW_SECONDS),
                        )
                        writer.write(bytes([0x20, 0x02, 0x00, 0x05]))  # Not authorized
                        await writer.drain()
                        break
                    authenticated, keep_alive = await self._handle_connect(payload, writer)
                    if not authenticated:
                        self._record_auth_failure(source_ip)
                        break
                    self._clear_auth_failures(source_ip)
                    # Drop the application-level read timeout; rely on
                    # SO_KEEPALIVE below for dead-connection detection. Real
                    # Bambu firmware does the same — accept any negotiated
                    # keepalive but never enforce §4.4's 1.5× disconnect on the
                    # otherwise-idle MQTT session (#1548 round 2). keep_alive is
                    # logged for support bundles but no longer drives a disconnect.
                    read_timeout = None
                    logger.info(
                        "%sMQTT client %s authenticated (negotiated keepalive=%ds, idle disconnect disabled)",
                        self._log_prefix,
                        client_id,
                        keep_alive,
                    )
                    # Enable TCP keepalive so a hard network drop is detected by
                    # the OS within a few minutes rather than waiting for the
                    # next outbound write to ECONNRESET.
                    #
                    # Also tighten the Linux keepalive schedule. Defaults are
                    # tcp_keepalive_time=7200 s (2 h before first probe),
                    # tcp_keepalive_intvl=75, tcp_keepalive_probes=9 — so a macOS
                    # client that goes to sleep silently is only detected as dead
                    # ~2 h 11 min later, and until then the push loop keeps stalling
                    # on drain-timeouts to the zombie socket. #1872: a P1S sleep/wake
                    # left the pre-sleep session in _clients for 5+ min with no
                    # eviction signal. New settings (idle=60 s, interval=15 s,
                    # count=4) detect a dead peer in ~2 min. ``getattr`` guards keep
                    # this cross-platform — macOS has TCP_KEEPINTVL but not
                    # TCP_KEEPIDLE (uses TCP_KEEPALIVE); other platforms silently skip.
                    sock = writer.get_extra_info("socket")
                    if sock is not None:
                        try:
                            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                        except OSError as e:
                            logger.debug("%sFailed to set SO_KEEPALIVE on %s: %s", self._log_prefix, client_id, e)
                        for opt_name, opt_value in (
                            ("TCP_KEEPIDLE", 60),
                            ("TCP_KEEPINTVL", 15),
                            ("TCP_KEEPCNT", 4),
                        ):
                            opt = getattr(socket, opt_name, None)
                            if opt is None:
                                continue
                            try:
                                sock.setsockopt(socket.IPPROTO_TCP, opt, opt_value)
                            except OSError as e:
                                logger.debug(
                                    "%sFailed to set %s=%s on %s: %s",
                                    self._log_prefix,
                                    opt_name,
                                    opt_value,
                                    client_id,
                                    e,
                                )
                    # Register client; start with self.serial until we learn
                    # the slicer's preferred serial from SUBSCRIBE/PUBLISH.
                    self._clients[client_id] = writer
                    self._client_serials[client_id] = self.serial
                elif packet_type == 3:  # PUBLISH
                    if authenticated:
                        await self._handle_publish(header[0], payload, writer, client_id)
                elif packet_type == 8:  # SUBSCRIBE
                    if authenticated:
                        await self._handle_subscribe(payload, writer, client_id)
                elif packet_type == 12:  # PINGREQ
                    # Send PINGRESP
                    writer.write(bytes([0xD0, 0x00]))
                    await writer.drain()
                elif packet_type == 14:  # DISCONNECT
                    break

        except asyncio.CancelledError:
            pass  # Expected when server is shutting down and cancels client tasks
        except Exception as e:
            # WARNING, not DEBUG: this outer catch only sees UNEXPECTED errors
            # (normal disconnects break the loop without raising). Production
            # defaults suppress DEBUG, so "slicer disconnects randomly" reports
            # arrived with no signal — surface it (upstream Bambuddy v0.2.4.5).
            logger.warning("%sMQTT client error from %s: %s", self._log_prefix, client_id, e)
        finally:
            logger.debug("MQTT client disconnected: %s", client_id)
            self._clients.pop(client_id, None)
            self._client_serials.pop(client_id, None)
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass  # Best-effort socket cleanup on client disconnect

    async def _read_remaining_length(self, reader: asyncio.StreamReader) -> int | None:
        """Read MQTT remaining length (variable byte integer)."""
        multiplier = 1
        value = 0

        for _ in range(4):
            try:
                byte = await reader.read(1)
                if not byte:
                    return None
                encoded = byte[0]
                value += (encoded & 127) * multiplier
                if (encoded & 128) == 0:
                    return value
                multiplier *= 128
            except OSError:
                return None

        return None

    def _is_auth_rate_limited(self, source_ip: str) -> bool:
        """Return True if ``source_ip`` has hit the per-IP failure cap.

        Prunes timestamps older than the window so ``_auth_failures`` can't grow
        unbounded. Uses ``time.monotonic()`` so a wall-clock jump can't extend
        or shorten the window.
        """
        now = time.monotonic()
        window_start = now - _AUTH_RATE_LIMIT_WINDOW_SECONDS
        recent = [t for t in self._auth_failures.get(source_ip, []) if t >= window_start]
        if recent:
            self._auth_failures[source_ip] = recent
        else:
            self._auth_failures.pop(source_ip, None)
        return len(recent) >= _AUTH_RATE_LIMIT_MAX_ATTEMPTS

    def _record_pending_request(self, data: dict, client_id: str) -> None:
        """Stash sequence_id → client_id for any nested block carrying a seq id.

        Slicer commands wrap the seq id in ``{"print": {...}}`` / ``{"info": ...}``
        etc. Walks the top-level dict values once; if no seq id is present the
        response just falls through to broadcast (fine for unsolicited pushes).
        FIFO-evicts at the cap so an unconsumed-response slicer can't leak memory.
        """
        for block in data.values():
            if isinstance(block, dict):
                seq = block.get("sequence_id")
                if seq is not None:
                    key = str(seq)
                    while len(self._pending_requests) >= _PENDING_REQUEST_MAX_ENTRIES:
                        oldest = next(iter(self._pending_requests))
                        self._pending_requests.pop(oldest, None)
                    self._pending_requests[key] = client_id
                    return

    def _lookup_pending_request_client(self, payload: bytes) -> str | None:
        """Return the client_id that issued the request this payload answers.

        ``None`` for printer-initiated pushes (no recorded seq id) so
        ``push_raw_to_clients`` falls back to broadcast — required for
        push_status and the other unsolicited pushes every slicer expects.
        """
        try:
            parsed = json.loads(payload)
        except (ValueError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        for block in parsed.values():
            if isinstance(block, dict):
                seq = block.get("sequence_id")
                if seq is not None:
                    return self._pending_requests.pop(str(seq), None)
        return None

    def _record_auth_failure(self, source_ip: str) -> None:
        """Append a timestamp for ``source_ip``'s failed auth attempt."""
        self._auth_failures.setdefault(source_ip, []).append(time.monotonic())

    def _clear_auth_failures(self, source_ip: str) -> None:
        """Reset ``source_ip``'s failure history after a successful auth."""
        self._auth_failures.pop(source_ip, None)

    async def _handle_connect(self, payload: bytes, writer: asyncio.StreamWriter) -> tuple[bool, int]:
        """Handle MQTT CONNECT packet.

        Returns ``(authenticated, keep_alive_seconds)`` — the caller's read loop
        honours the negotiated keepalive instead of a hardcoded 60 s. ``0`` means
        the client opted out of keepalive (MQTT spec §3.1.2.10).
        """
        try:
            # Parse CONNECT packet
            # Skip protocol name length and name
            idx = 0
            proto_len = (payload[idx] << 8) | payload[idx + 1]
            idx += 2 + proto_len

            # Skip protocol level and connect flags
            # connect_flags = payload[idx + 1]
            idx += 2

            # Keepalive (2-byte big-endian, seconds). Honoured by the read loop
            # per MQTT spec §4.4 — before #1548 we ignored it and used a
            # hardcoded 60 s, which closed OrcaSlicer's idle connection at the
            # negotiated boundary instead of the spec-mandated 1.5×.
            keep_alive = (payload[idx] << 8) | payload[idx + 1]
            idx += 2

            # Read client ID
            client_id_len = (payload[idx] << 8) | payload[idx + 1]
            idx += 2
            # client_id = payload[idx : idx + client_id_len].decode("utf-8")
            idx += client_id_len

            # Read username
            username_len = (payload[idx] << 8) | payload[idx + 1]
            idx += 2
            username = payload[idx : idx + username_len].decode("utf-8")
            idx += username_len

            # Read password
            password_len = (payload[idx] << 8) | payload[idx + 1]
            idx += 2
            password = payload[idx : idx + password_len].decode("utf-8")

            # Authenticate. ``hmac.compare_digest`` is constant-time so the auth
            # check can't leak the access code via response timing.
            if username == "bblp" and hmac.compare_digest(password, self.access_code):
                # Send CONNACK with success
                writer.write(bytes([0x20, 0x02, 0x00, 0x00]))
                await writer.drain()
                logger.info("%sMQTT client authenticated successfully", self._log_prefix)

                # Send immediate status report after auth - slicer expects this
                await self._send_status_report(writer)
                return True, keep_alive
            else:
                # Send CONNACK with auth failure
                writer.write(bytes([0x20, 0x02, 0x00, 0x05]))  # Not authorized
                await writer.drain()
                logger.warning("%sMQTT auth failed for user '%s' (access code mismatch)", self._log_prefix, username)
                return False, 0

        except (IndexError, ValueError) as e:
            logger.debug("MQTT CONNECT parse error: %s", e)
            # Send CONNACK with error
            writer.write(bytes([0x20, 0x02, 0x00, 0x02]))  # Protocol error
            await writer.drain()
            return False, 0

    async def _handle_subscribe(self, payload: bytes, writer: asyncio.StreamWriter, client_id: str) -> None:
        """Handle MQTT SUBSCRIBE packet."""
        try:
            # Parse packet ID
            packet_id = (payload[0] << 8) | payload[1]

            # Parse topic filters (just acknowledge them)
            idx = 2
            granted_qos = []
            learned_serial: str | None = None
            while idx < len(payload):
                topic_len = (payload[idx] << 8) | payload[idx + 1]
                idx += 2
                topic = payload[idx : idx + topic_len].decode("utf-8")
                idx += topic_len
                requested_qos = payload[idx]
                idx += 1

                logger.info("%sMQTT subscribe: %s QoS=%s", self._log_prefix, topic, requested_qos)
                granted_qos.append(min(requested_qos, 1))  # Grant up to QoS 1

                # Learn the serial the slicer is listening on
                if learned_serial is None:
                    extracted = self._extract_serial_from_topic(topic)
                    if extracted:
                        learned_serial = extracted

            if learned_serial and learned_serial != self._client_serials.get(client_id):
                if learned_serial != self.serial:
                    logger.info(
                        "%sMQTT client subscribed with serial %s (VP serial is %s) - adapting responses",
                        self._log_prefix,
                        learned_serial,
                        self.serial,
                    )
                self._client_serials[client_id] = learned_serial

            # Send SUBACK
            suback = bytes([0x90, 2 + len(granted_qos), packet_id >> 8, packet_id & 0xFF])
            suback += bytes(granted_qos)
            writer.write(suback)
            await writer.drain()

            # Send initial status report on the client's subscribed topic
            await self._send_status_report(writer, serial=self._client_serials.get(client_id, self.serial))

        except (IndexError, ValueError, OSError) as e:
            logger.debug("MQTT SUBSCRIBE error: %s", e)

    async def _send_status_report(self, writer: asyncio.StreamWriter, serial: str | None = None) -> None:
        """Send a status report to the slicer. Uses client's serial if provided.

        When a bridge is active and has cached the real printer's latest
        push_status, send a copy of the real push with only the upload-state-
        machine fields we own (``gcode_state``, ``gcode_file``,
        ``prepare_percent``, ``subtask_name``) overridden. BambuStudio's Send
        pre-flight checks the push_status shape against what it expects from
        the printer model, and the synthetic stub introduces fields the real
        H2D doesn't have (``storage``, the wrong ``chamber_temper`` shape, …)
        which trip the check.
        """
        try:
            self._sequence_id += 1
            reported_state = self._reported_gcode_state()

            cached = self._bridge.get_latest_print_state() if self._bridge is not None else None
            if isinstance(cached, dict):
                # Real-printer-shaped response. Copy the cache, then replace
                # the protocol / upload-state fields with values under our
                # control.
                print_block = dict(cached)
                print_block["sequence_id"] = str(self._sequence_id)
                print_block["command"] = "push_status"
                print_block["msg"] = 0
                print_block["gcode_state"] = reported_state
                print_block["gcode_file"] = self._current_file
                print_block["gcode_file_prepare_percent"] = self._prepare_percent
                if self._current_file:
                    print_block["subtask_name"] = self._current_file.replace(".3mf", "")
                else:
                    # Don't override real subtask_name with empty if no upload pending.
                    print_block.setdefault("subtask_name", "")
                # Zero the live-progress activity fields (#1558). Forcing
                # gcode_state=IDLE above isn't enough: BambuStudio's Send
                # pre-flight also reads mc_percent / stg_cur / layer_num / … and,
                # if the real printer is mid-print, the cached-as-base path let
                # those leak through — the slicer read them as "busy" and refused
                # the Send even though gcode_state said IDLE. The VP is always
                # idle from the slicer's perspective, so overlay the whole set.
                print_block["mc_print_stage"] = ""
                print_block["mc_percent"] = 0
                print_block["mc_remaining_time"] = 0
                print_block["stg"] = []
                print_block["stg_cur"] = 0
                print_block["layer_num"] = 0
                print_block["total_layer_num"] = 0
                print_block["print_error"] = 0
                # Storage indicators overlay — the synthetic stub below always
                # bakes these three fields because BambuStudio's Send pre-flight
                # reads them; the cached-as-base path used to pass the real
                # printer's push through with only an IP rewrite, and P1S/A1
                # firmware that doesn't report them tripped the pre-flight with
                # a generic "storage needs to be inserted before send to
                # printer" error before any FTP attempt (#1228, upstream
                # ceffcfae). For VP usage the slicer FTPs to BamDude, so the
                # printer's actual SD card is irrelevant on the queue/immediate/
                # review/auto-queue cached-as-base paths — forcing "storage
                # available" is correct. setdefault preserves real values when
                # present (real home_flag bits stay on; real storage block
                # passes through unchanged), so the overlay never overrides
                # what the printer actually reported.
                print_block["home_flag"] = (print_block.get("home_flag") or 0) | 0x100
                print_block["sdcard"] = True
                print_block.setdefault("storage", {"free": 1_000_000_000, "total": 32_000_000_000})
                status = {"print": print_block}
                await self._publish_to_report(writer, status, serial or self.serial)
                return

            # No bridge / no cache yet — fall back to the synthetic stub.
            status = {
                "print": {
                    "sequence_id": str(self._sequence_id),
                    "command": "push_status",
                    "msg": 0,
                    "gcode_state": reported_state,
                    "gcode_file": self._current_file,
                    "gcode_file_prepare_percent": self._prepare_percent,
                    "subtask_name": self._current_file.replace(".3mf", "") if self._current_file else "",
                    "mc_print_stage": "",
                    "mc_percent": 0,
                    "mc_remaining_time": 0,
                    "wifi_signal": "-44dBm",
                    "print_error": 0,
                    "print_type": "",
                    "bed_temper": 25.0,
                    "bed_target_temper": 0.0,
                    "nozzle_temper": 25.0,
                    "nozzle_target_temper": 0.0,
                    "chamber_temper": 25.0,
                    "cooling_fan_speed": "0",
                    "big_fan1_speed": "0",
                    "big_fan2_speed": "0",
                    "heatbreak_fan_speed": "0",
                    "spd_lvl": 1,
                    "spd_mag": 100,
                    "stg": [],
                    "stg_cur": 0,
                    "layer_num": 0,
                    "total_layer_num": 0,
                    "home_flag": 256,  # Bit 8 = SD card present (HAS_SDCARD_NORMAL)
                    "hw_switch_state": 0,
                    "online": {"ahb": False, "rfid": False, "version": 7},
                    "ams_status": 0,
                    "sdcard": True,
                    "storage": {"free": 1000000000, "total": 32000000000},
                    "upgrade_state": {
                        "sequence_id": 0,
                        "progress": "",
                        "status": "",
                        "consistency_request": False,
                        "dis_state": 0,
                        "err_code": 0,
                        "force_upgrade": False,
                        "message": "",
                        "module": "",
                        "new_version_state": 2,
                        "new_ver_list": [],
                        "ota_new_version_number": "",
                        "ahb_new_version_number": "",
                    },
                    "ipcam": {
                        "ipcam_dev": "1",
                        "ipcam_record": "enable",
                        "timelapse": "disable",
                        "resolution": "1080p",
                        "mode_bits": 0,
                    },
                    "xcam": {
                        "allow_skip_parts": False,
                        "buildplate_marker_detector": True,
                        "first_layer_inspector": True,
                        "halt_print_sensitivity": "medium",
                        "print_halt": True,
                        "printing_monitor": True,
                        "spaghetti_detector": True,
                    },
                    "lights_report": [{"node": "chamber_light", "mode": "on"}],
                    "nozzle_diameter": "0.4",
                    "nozzle_type": "hardened_steel",
                }
            }

            await self._publish_to_report(writer, status, serial or self.serial)

        except OSError as e:
            logger.error("Failed to send status report: %s", e)

    async def _send_version_response(
        self, writer: asyncio.StreamWriter, sequence_id: str, serial: str | None = None
    ) -> None:
        """Send version info response to the slicer."""
        try:
            product_name = MODEL_PRODUCT_NAMES.get(self.model, self.model or "X1 Carbon")
            serial = serial or self.serial

            # Build version response matching OrcaSlicer expectations
            # Required fields per module: name, product_name, sw_ver, sw_new_ver, sn, hw_ver, flag
            version_info = {
                "info": {
                    "command": "get_version",
                    "sequence_id": sequence_id,
                    "module": [
                        {
                            "name": "ota",
                            "product_name": product_name,
                            "sw_ver": "01.07.00.00",
                            "sw_new_ver": "",
                            "hw_ver": "OTA",
                            "sn": serial,
                            "flag": 0,
                        },
                        {
                            "name": "esp32",
                            "product_name": product_name,
                            "sw_ver": "01.07.22.25",
                            "sw_new_ver": "",
                            "hw_ver": "AP05",
                            "sn": serial,
                            "flag": 0,
                        },
                        {
                            "name": "rv1126",
                            "product_name": product_name,
                            "sw_ver": "00.00.27.38",
                            "sw_new_ver": "",
                            "hw_ver": "AP05",
                            "sn": serial,
                            "flag": 0,
                        },
                        {
                            "name": "th",
                            "product_name": product_name,
                            "sw_ver": "00.00.04.00",
                            "sw_new_ver": "",
                            "hw_ver": "TH07",
                            "sn": serial,
                            "flag": 0,
                        },
                        {
                            "name": "mc",
                            "product_name": product_name,
                            "sw_ver": "00.00.10.00",
                            "sw_new_ver": "",
                            "hw_ver": "MC07",
                            "sn": serial,
                            "flag": 0,
                        },
                    ],
                }
            }

            # Overlay real version modules from the bridge cache when available
            # — specifically the AMS modules (``ams/0``, ``n3f/0``, ``n3s/128``,
            # …) that BambuStudio's Prepare tab uses to identify AMS hardware.
            # Without them every AMS unit shows as "unknown" in the Prepare panel.
            if self._bridge is not None:
                cached_modules = self._bridge.get_latest_version_modules()
                if isinstance(cached_modules, list) and cached_modules:
                    version_info["info"]["module"] = cached_modules

            await self._publish_to_report(writer, version_info, serial)
            logger.info("Sent version response (product_name=%s)", product_name)

        except OSError as e:
            logger.error("Failed to send version response: %s", e)

    def set_gcode_state(self, state: str, filename: str = "", prepare_percent: str = "0") -> None:
        """Update the gcode state reported to connected slicers.

        Called by the manager to reflect FTP upload progress/completion.
        """
        self._gcode_state = state
        self._current_file = filename
        self._prepare_percent = prepare_percent

    def upload_started(self) -> None:
        """Mark that an FTP upload began (called by the FTP server)."""
        self._active_uploads += 1

    def upload_finished(self) -> None:
        """Mark that an FTP upload ended — on success OR failure. Paired with
        ``upload_started`` from the FTP STOR handler's ``finally``."""
        if self._active_uploads > 0:
            self._active_uploads -= 1

    def resolve_stale_prepare(self) -> None:
        """Drop a leftover ``PREPARE`` to ``IDLE`` when there's nothing to print.

        Used after a non-3MF upload completes (cover image / slicer junk): the
        ``project_file`` command flipped us to ``PREPARE`` but no print job
        exists, so advance to a terminal idle state rather than leaving the
        slicer reading "busy with another print job"."""
        if self._gcode_state == "PREPARE":
            self._gcode_state = "IDLE"
            self._current_file = ""
            self._prepare_percent = "0"

    def _reported_gcode_state(self) -> str:
        """The ``gcode_state`` to advertise in status pushes.

        ``PREPARE`` is only legitimate while an upload is actually in flight.
        A ``PREPARE`` left from a ``project_file`` whose upload never completed
        (slicer cancelled, FTP transfer failed) makes every slicer's pre-flight
        read "busy with another print job", because BambuStudio and OrcaSlicer
        alike map ``gcode_state`` → ``print_status`` and treat ``PREPARE`` as
        in-printing. When no upload is in flight, report ``IDLE`` so the slicer
        can send again — unless we're still inside the brief grace right after a
        ``project_file`` command, during which the slicer is opening its FTP
        connection and the PREPARE is genuinely live (not yet stale)."""
        if self._gcode_state == "PREPARE" and self._active_uploads == 0:
            if time.monotonic() - self._prepare_set_monotonic <= PREPARE_GRACE_SECONDS:
                return "PREPARE"
            return "IDLE"
        return self._gcode_state

    async def _publish_to_report(self, writer: asyncio.StreamWriter, payload: dict, serial: str = "") -> None:
        """Publish a message on the device report topic.

        Real Bambu printers wire-format push_status JSON with 4-space
        indentation (32254 bytes for an idle H2D push vs 14268 bytes
        compact). BambuStudio's Send pre-flight rejects compact JSON —
        without matching the on-wire format the slicer never proceeds to
        FTP upload.
        """
        topic = f"device/{serial or self.serial}/report"
        message = json.dumps(payload, indent=4)

        topic_bytes = topic.encode("utf-8")
        message_bytes = message.encode("utf-8")

        remaining = 2 + len(topic_bytes) + len(message_bytes)
        packet = bytes([0x30])  # PUBLISH, QoS 0

        while remaining > 0:
            byte = remaining % 128
            remaining //= 128
            if remaining > 0:
                byte |= 0x80
            packet += bytes([byte])

        packet += bytes([len(topic_bytes) >> 8, len(topic_bytes) & 0xFF])
        packet += topic_bytes
        packet += message_bytes

        writer.write(packet)
        # Timeout the drain to prevent blocking the event loop if the
        # MQTT client stops reading (e.g. slicer busy with FTP upload,
        # macOS suspends the client mid-session — #1872).
        #
        # On timeout, close the writer and raise BrokenPipeError so the
        # push-loop's ``except OSError`` at ``_periodic_status_push`` evicts
        # the client from ``self._clients`` on this same tick (and the closed
        # writer's ``is_closing()`` evicts it on the next tick regardless).
        # Before this, timeouts logged at DEBUG and returned silently, so the
        # zombie writer sat in ``self._clients`` until SO_KEEPALIVE detected the
        # dead peer (~2 h on Linux defaults), while the push loop spent 5 s per
        # iteration on the stalled client.
        try:
            await asyncio.wait_for(writer.drain(), timeout=5)
        except TimeoutError as e:
            logger.info(
                "%sMQTT drain timeout for %s — closing stalled writer",
                self._log_prefix,
                topic,
            )
            try:
                writer.close()
            except Exception:
                pass  # best-effort — writer may already be broken
            raise BrokenPipeError(f"drain timeout on {topic}") from e

    async def _send_print_response(
        self, writer: asyncio.StreamWriter, sequence_id: str, filename: str, serial: str | None = None
    ) -> None:
        """Send project_file acknowledgment matching real Bambu printer behavior."""
        # Update state so periodic status pushes reflect preparation. Stamp the
        # time so ``_reported_gcode_state`` keeps advertising PREPARE during the
        # short window before the slicer opens its FTP upload connection.
        self._gcode_state = "PREPARE"
        self._current_file = filename
        self._prepare_percent = "0"
        self._prepare_set_monotonic = time.monotonic()

        try:
            # Send command acknowledgment - slicer expects to see
            # command: "project_file" echoed back before starting FTP upload
            subtask_name = filename.replace(".3mf", "") if filename else ""
            response = {
                "print": {
                    "command": "project_file",
                    "sequence_id": sequence_id,
                    "param": "Metadata/plate_1.gcode",
                    "subtask_name": subtask_name,
                    "gcode_state": "PREPARE",
                    "gcode_file": filename,
                    "gcode_file_prepare_percent": "0",
                    "result": "SUCCESS",
                    "msg": 0,
                }
            }
            await self._publish_to_report(writer, response, serial or self.serial)
            logger.info("Sent project_file acknowledgment for %s", filename)
        except OSError as e:
            logger.error("Failed to send print response: %s", e)

    async def _handle_publish(self, header: int, payload: bytes, writer: asyncio.StreamWriter, client_id: str) -> None:
        """Handle MQTT PUBLISH packet. Serial-adaptive: accepts any device/*/request topic."""
        try:
            # Parse topic
            idx = 0
            topic_len = (payload[idx] << 8) | payload[idx + 1]
            idx += 2
            topic = payload[idx : idx + topic_len].decode("utf-8")
            idx += topic_len

            # Check for packet ID (QoS > 0)
            qos = (header & 0x06) >> 1
            if qos > 0:
                idx += 2

            # Parse message
            message = payload[idx:].decode("utf-8")

            logger.info("MQTT publish to %s: %s...", topic, message[:100])

            # Only handle device/.../request topics (client already authenticated)
            if not topic.startswith("device/") or "/request" not in topic:
                return

            # Learn the serial the slicer is actually using
            client_serial = self._extract_serial_from_topic(topic) or self.serial
            if client_serial and client_serial != self._client_serials.get(client_id):
                if client_serial != self.serial:
                    logger.info(
                        "%sMQTT client publishing with serial %s (VP serial is %s) - adapting responses",
                        self._log_prefix,
                        client_serial,
                        self.serial,
                    )
                self._client_serials[client_id] = client_serial

            try:
                # Some slicer builds (observed with OrcaSlicer on Linux, #927)
                # include the C-string null terminator in the MQTT payload
                # length, so the decoded message ends with \x00. Real brokers
                # pass the bytes through; strict json.loads raises "Extra data"
                # and every pushall/get_version/project_file silently dropped.
                data = json.loads(message.rstrip("\x00 \r\n\t"))
            except json.JSONDecodeError as e:
                logger.debug(
                    "MQTT publish JSON decode failed: %s (payload=%r)",
                    e,
                    message[:200],
                )
                return

            # Record sequence_id → this client so a bridge-forwarded response
            # routes back only to the slicer that issued the command (V5).
            self._record_pending_request(data, client_id)

            # The synthetic flow below is the original (pre-bridge) behaviour
            # and is what the proven-working FTP "Send" depends on. Do NOT
            # replace any synthetic response with a forward — only ADD
            # forwarding alongside, at the bottom, for commands the synthetic
            # flow doesn't handle (AMS write / xcam / system / etc., which
            # need to actually reach the real printer).
            handled_locally = False

            # Handle pushing command (status request)
            if "pushing" in data:
                pushing_data = data["pushing"]
                command = pushing_data.get("command", "")
                logger.info("MQTT pushing command: %s", command)

                if command == "pushall":
                    logger.info("Sending status report in response to pushall")
                    await self._send_status_report(writer, serial=client_serial)
                    handled_locally = True
                elif command == "start":
                    logger.info("Starting status push stream")
                    await self._send_status_report(writer, serial=client_serial)
                    handled_locally = True

            # Handle info commands (get_version, etc.)
            if "info" in data:
                info_data = data["info"]
                command = info_data.get("command", "")
                sequence_id = info_data.get("sequence_id", "0")
                logger.info("MQTT info command: %s", command)

                if command == "get_version":
                    await self._send_version_response(writer, sequence_id, serial=client_serial)
                    handled_locally = True

            # Handle print commands
            if "print" in data:
                print_data = data["print"]
                command = print_data.get("command", "")
                filename = print_data.get("subtask_name", "")
                sequence_id = print_data.get("sequence_id", "0")

                logger.info("MQTT print command: %s for %s", command, filename)

                if command in ("project_file", "gcode_file"):
                    # File lives on BamDude, not the printer — synthetic only.
                    file_3mf = print_data.get("file", filename)
                    await self._send_print_response(writer, sequence_id, file_3mf, serial=client_serial)

                    if self.on_print_command:
                        # ``filename`` is the slicer's ``subtask_name`` (bare
                        # model name, no extension) — passed through verbatim so
                        # the ``_schedule_finish_release`` chain echoes it back
                        # as gcode_file + subtask_name in push_status and the
                        # slicer matches against its own subtask_name. The FTP
                        # filename (WITH extension) rides in ``print_data["file"]``
                        # for ``on_print_command`` to use as its queue-stash key,
                        # matching ``_add_to_print_queue``'s lookup (#1780).
                        await self._notify_print_command(filename, print_data)
                    handled_locally = True

            # Forward anything the synthetic flow didn't handle to the real
            # printer. AMS load / dry / xcam / system / extrusion_cali_get etc.
            if not handled_locally and self._bridge is not None and self._bridge.is_active:
                self._bridge.forward_to_printer(data)

        except (IndexError, ValueError, OSError) as e:
            logger.debug("MQTT PUBLISH error: %s", e)

    async def _notify_print_command(self, filename: str, data: dict) -> None:
        """Notify callback of print command."""
        if self.on_print_command:
            try:
                result = self.on_print_command(filename, data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error("Print command callback error: %s", e)
