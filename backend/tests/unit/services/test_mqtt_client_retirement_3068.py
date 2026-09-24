"""Letting go of a paho client must never block the thread that let it go.

Upstream Bambuddy #3068: a printer that had been offline 38 hours still answered
on 8883. The connection watchdog rebuilt its session, which ended in paho's
``loop_stop()`` -- set a terminate flag, then ``join()`` the network thread with
no timeout. The network thread was parked in ``reconnect()``'s TLS handshake,
where it cannot read that flag, and a socket timeout is per operation, renewed
by every byte a trickling peer sends. The join ran on the asyncio thread: the
process stayed up while UI, API and ``/health`` stopped answering.

In BamDude the same join sat on more paths than upstream's: the connection
watchdog, ``force_reconnect_stale_session`` (queue dispatch deadline,
``check_staleness`` on an ordinary status poll), the printer routes'
``disconnect()``, ``connect()``'s own retirement of a still-live client, and
the relay / smart-plug shutdown.
"""

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient

# Generous for a slow CI runner, and still far below the 10 s a wedged fake
# holds its join -- a regression reads as ~10 s, not as a borderline miss.
_PROMPT_SECONDS = 2.0


class WedgedPahoClient:
    """A paho client whose network thread will not stop.

    ``loop_stop()`` blocks until ``release()`` (or 10 s), which is what a real
    one does while its thread sits in ``do_handshake()`` against a printer that
    answers TCP and then goes quiet.
    """

    def __init__(self):
        self.released = threading.Event()
        self.disconnect_called = threading.Event()
        self.loop_stop_returned = threading.Event()
        self.on_connect = "sentinel"
        self.on_disconnect = "sentinel"
        self.on_subscribe = "sentinel"
        self.on_message = "sentinel"

    def disconnect(self):
        self.disconnect_called.set()

    def loop_stop(self):
        self.released.wait(timeout=10)
        self.loop_stop_returned.set()

    def release(self):
        self.released.set()


@pytest.fixture
def client():
    return BambuMQTTClient(ip_address="192.0.2.10", access_code="12345678", serial_number="00M09A123456789")


class TestHardReset:
    def test_it_does_not_wait_for_the_old_network_thread(self, client):
        wedged = WedgedPahoClient()
        client._client = wedged
        client._loop = None  # no rebuild, so only the teardown is measured

        try:
            started = time.monotonic()
            client._hard_reset_client()
            elapsed = time.monotonic() - started
        finally:
            wedged.release()

        assert elapsed < _PROMPT_SECONDS, f"_hard_reset_client blocked for {elapsed:.2f}s (#3068)"
        assert client._client is None

    def test_the_old_client_can_no_longer_reach_this_object(self, client):
        """⚠️ The join used to guarantee that a client we let go of could not
        touch our state. With the teardown detached, a zombie that finishes
        its handshake would auto-reconnect and report itself connected behind
        its replacement's back -- so the callbacks go, inline."""
        wedged = WedgedPahoClient()
        client._client = wedged
        client._loop = None

        try:
            client._hard_reset_client()
            detached = (wedged.on_connect, wedged.on_disconnect, wedged.on_subscribe, wedged.on_message)
        finally:
            wedged.release()

        assert detached == (None, None, None, None)

    def test_the_old_session_is_still_disconnected_and_stopped(self, client):
        """DISCONNECT is what stops paho's auto-reconnect, and with it an unacked
        ``project_file`` replaying onto a revived session (#1136). Handing the
        teardown off must not mean skipping it."""
        wedged = WedgedPahoClient()
        client._client = wedged
        client._loop = None

        client._hard_reset_client()

        assert wedged.disconnect_called.wait(timeout=5), "the old client was never disconnected"
        wedged.release()
        assert wedged.loop_stop_returned.wait(timeout=5), "the old client's loop was never stopped"

    def test_the_replacement_gets_a_fresh_client_id(self, client):
        """#1136: the new session must not inherit paho's QoS 1 queue, which is
        what a new client_id buys."""
        wedged = WedgedPahoClient()
        client._client = wedged
        client._loop = MagicMock()

        with patch("backend.app.services.bambu_mqtt.mqtt.Client") as MockClient:
            MockClient.return_value = MagicMock()
            try:
                client._hard_reset_client()
            finally:
                wedged.release()

        assert MockClient.call_count == 1
        assert client.serial_number in MockClient.call_args.kwargs["client_id"]
        assert client._client is MockClient.return_value

    async def test_a_wedged_printer_does_not_stall_the_event_loop(self, client):
        """The reported failure, end to end: ``force_reconnect_stale_session`` is
        what the queue dispatch deadline calls from a coroutine. A heartbeat has
        to keep ticking through it."""
        wedged = WedgedPahoClient()
        client._client = wedged
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            with patch("backend.app.services.bambu_mqtt.mqtt.Client") as MockClient:
                MockClient.return_value = MagicMock()
                started = time.monotonic()
                client.force_reconnect_stale_session("offline for 900s, port still answering")
                elapsed = time.monotonic() - started
            await asyncio.sleep(0.1)
        finally:
            beat.cancel()
            wedged.release()
            try:
                await beat
            except asyncio.CancelledError:
                pass

        assert elapsed < _PROMPT_SECONDS, f"the forced reconnect held the event loop for {elapsed:.2f}s (#3068)"
        assert ticks > 0, "the event loop made no progress while the old client was stopping"
        assert client.state.connected is False


class TestConnectRetiringALiveClient:
    """BamDude's own path, not upstream's: ``connect()`` retires a client that is
    still live (the 2026-08-21 outage guard). It runs on the event loop too."""

    def test_it_does_not_wait_for_the_old_network_thread(self, client):
        wedged = WedgedPahoClient()
        client._client = wedged

        with patch("backend.app.services.bambu_mqtt.mqtt.Client") as MockClient:
            MockClient.return_value = MagicMock()
            try:
                started = time.monotonic()
                client.connect()
                elapsed = time.monotonic() - started
            finally:
                wedged.release()

        assert elapsed < _PROMPT_SECONDS, f"connect() blocked on the old client for {elapsed:.2f}s (#3068)"
        assert client._client is MockClient.return_value
        assert wedged.on_disconnect is None


class TestDisconnect:
    def test_it_does_not_wait_for_the_old_network_thread(self, client):
        """Reached from the printer routes (edit, delete, hand-disconnect) and
        from the connection watchdog via ``disconnect_printer``."""
        wedged = WedgedPahoClient()
        client._client = wedged
        client.state.connected = True

        try:
            started = time.monotonic()
            client.disconnect()
            elapsed = time.monotonic() - started
        finally:
            wedged.release()

        assert elapsed < _PROMPT_SECONDS, f"disconnect() blocked for {elapsed:.2f}s (#3068)"
        assert client._client is None
        assert client.state.connected is False

    def test_the_disconnect_callback_still_gets_its_window(self, client):
        """The callback that releases the timeout fires on paho's thread, so it
        has to run before the retirement detaches it -- otherwise every caller
        with a non-zero timeout waits the timeout out in full."""

        class AnsweringClient(WedgedPahoClient):
            def disconnect(self):
                super().disconnect()
                if callable(self.on_disconnect):
                    self.on_disconnect(self, None)

        answering = AnsweringClient()
        answering.on_disconnect = client._on_disconnect  # as connect() wires it
        client._client = answering

        try:
            started = time.monotonic()
            client.disconnect(timeout=5)
            elapsed = time.monotonic() - started
        finally:
            answering.release()

        assert elapsed < _PROMPT_SECONDS, (
            f"disconnect(timeout=5) took {elapsed:.2f}s -- the callback was detached before it could report"
        )
        assert client._disconnection_event.is_set()

    def test_disconnecting_twice_is_harmless(self, client):
        wedged = WedgedPahoClient()
        client._client = wedged
        try:
            client.disconnect()
            client.disconnect()
        finally:
            wedged.release()

        assert client._client is None

    def test_a_hand_disconnected_printer_is_not_announced_as_offline(self, client):
        """``_on_disconnect`` suppresses itself for a clean disconnect of a printer
        that reported in the last 10 s, so a healthy printer disconnected on
        purpose never broadcast one. Announcing it now would reach the
        connected→disconnected edge and notify the user their printer went
        offline a minute after they disconnected it (#1752)."""
        seen = []
        client.on_state_change = seen.append
        client._last_message_time = time.time()
        wedged = WedgedPahoClient()
        client._client = wedged
        client.state.connected = True

        try:
            client.disconnect()
        finally:
            wedged.release()

        assert seen == [], "disconnecting a printer by hand announced it as offline"
        assert client.state.connected is False


class TestTheOtherMqttServices:
    """The relay and the smart-plug service tear their brokers down the same way,
    at shutdown. A wedged broker there does not stop request serving -- nothing
    is served by then -- but it stops the process from exiting, so the service
    manager has to kill it instead."""

    async def test_the_relay_does_not_wait_for_its_network_thread(self):
        from backend.app.services.mqtt_relay import MQTTRelayService

        wedged = WedgedPahoClient()
        service = MQTTRelayService()
        service.client = wedged
        service.connected = True

        try:
            started = time.monotonic()
            await service.disconnect()
            elapsed = time.monotonic() - started
        finally:
            wedged.release()

        assert elapsed < _PROMPT_SECONDS, f"relay shutdown blocked for {elapsed:.2f}s (#3068)"
        assert service.client is None
        # Only the retirement detaches callbacks, so this proves it ran rather
        # than the service's except-block swallowing an error.
        assert wedged.on_disconnect is None

    async def test_the_smart_plug_service_does_not_wait_for_its_network_thread(self):
        from backend.app.services.mqtt_smart_plug import MQTTSmartPlugService

        wedged = WedgedPahoClient()
        service = MQTTSmartPlugService()
        service.client = wedged
        service.connected = True

        try:
            started = time.monotonic()
            await service.disconnect()
            elapsed = time.monotonic() - started
        finally:
            wedged.release()

        assert elapsed < _PROMPT_SECONDS, f"smart-plug shutdown blocked for {elapsed:.2f}s (#3068)"
        assert service.client is None
        assert wedged.on_disconnect is None
