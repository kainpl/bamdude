"""A second connect must not leave the first client running.

⚠️ Measured on a live farm, 2026-08-21. From 18:42:38 onward every MQTT
disconnect was written to the log TWICE, in the same millisecond, per printer —
and not once in the fourteen hours before that. ``_on_disconnect`` logs one line
per invocation, so that is two paho clients calling back into one
``BambuMQTTClient``.

An abandoned paho client is not inert. It keeps its network thread, it keeps
``on_disconnect`` bound to this instance, and ``reconnect_on_failure`` defaults
to True — so it reconnects under its own client id while ``disconnect()``, which
only ever sees ``self._client``, can retire the newer one and nothing else. Every
session rebuild then added another. The fleet came back only when the process
was restarted.

Every caller did retire the old client first. That was a convention; this makes
it a property of ``connect()`` itself.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


@pytest.fixture
def client():
    return BambuMQTTClient(ip_address="192.0.2.10", access_code="x", serial_number="TESTSERIAL0001")


@pytest.fixture
def paho_clients():
    """Every ``mqtt.Client(...)`` this test makes, in creation order."""
    made = []

    def _factory(*_a, **_kw):
        made.append(MagicMock())
        return made[-1]

    with patch("backend.app.services.bambu_mqtt.mqtt.Client", side_effect=_factory):
        yield made


class TestConnectRetiresWhatItReplaces:
    def test_a_second_connect_stops_the_first_client(self, client, paho_clients, caplog):
        client.connect()
        first = paho_clients[0]

        with caplog.at_level(logging.WARNING):
            client.connect()

        assert len(paho_clients) == 2, "expected a fresh client for the second connect"
        first.disconnect.assert_called_once()
        first.loop_stop.assert_called_once()
        assert client._client is paho_clients[1]

    def test_it_says_so_loudly(self, client, paho_clients, caplog):
        """⚠️ A caller reaching this has a bug, and a silent repair hides it."""
        client.connect()
        with caplog.at_level(logging.WARNING):
            client.connect()

        assert "still live" in caplog.text

    def test_a_first_connect_is_quiet(self, client, paho_clients, caplog):
        with caplog.at_level(logging.WARNING):
            client.connect()

        assert "still live" not in caplog.text

    def test_retiring_survives_a_client_that_throws(self, client, paho_clients):
        """The old client is already broken half the time it needs retiring."""
        client.connect()
        paho_clients[0].disconnect.side_effect = OSError("socket is gone")
        paho_clients[0].loop_stop.side_effect = OSError("thread is gone")

        client.connect()  # must not raise

        assert client._client is paho_clients[1]

    def test_both_halves_are_done(self, client, paho_clients):
        """⚠️ Neither alone is enough.

        Without the DISCONNECT the broker holds the session until its keepalive
        lapses; without the ``loop_stop`` the thread stays up and reconnects.
        """
        client.connect()
        client.connect()

        assert paho_clients[0].disconnect.called
        assert paho_clients[0].loop_stop.called


class TestAFailedConnectSaysWhy:
    """⚠️ ``connection_watchdog`` reports ``_last_connect_error`` when a rebuild
    fails — and nothing ever wrote it. Five failed rebuilds during the outage
    all logged "Last connect error: none recorded", which is why the log could
    not answer what happened.
    """

    def test_a_refusal_is_recorded(self, client, paho_clients, caplog):
        client.connect()
        rc = MagicMock()
        rc.__eq__ = lambda _self, other: False  # not 0 — a refusal
        rc.__str__ = lambda _self: "Not authorised"

        with caplog.at_level(logging.WARNING):
            client._on_connect(paho_clients[0], None, {}, rc)

        assert client._last_connect_error == "Not authorised"
        assert "connect refused" in caplog.text
        assert client.state.connected is False

    def test_a_new_attempt_clears_the_previous_reason(self, client, paho_clients):
        """⚠️ The object outlives its connections. A reason left over from two
        attempts ago would be read back as the reason for this one."""
        client.connect()
        client._last_connect_error = "Not authorised"

        client.connect()

        assert client._last_connect_error is None

    def test_a_failure_before_the_socket_is_recorded_too(self, client):
        """Never reaches ``_on_connect``, so it has to be caught where it happens."""
        broken = MagicMock()
        broken.connect_async.side_effect = OSError("no route to host")

        with patch("backend.app.services.bambu_mqtt.mqtt.Client", return_value=broken), pytest.raises(OSError):
            client.connect()

        assert "no route to host" in (client._last_connect_error or "")
        assert "OSError" in client._last_connect_error


class TestTheRequestTopicVerdictNeedsTwoStrikes:
    """⚠️ This heuristic cannot tell a broker refusing the subscription from the
    network dying three seconds later — and its verdict lasts until the process
    restarts.

    On 2026-08-21 a fleet-wide outage made every reconnect die young, and five
    printers that had been perfectly happy with the request topic had it
    switched off by an event that had nothing to do with them. A printer that
    genuinely refuses does it every time, so it still latches — one reconnect
    later.
    """

    @staticmethod
    def _died_right_after_subscribing(c):
        import time as _time

        c._request_topic_sub_time = _time.time()
        c._request_topic_confirmed = False
        c._last_message_time = 0
        rc = MagicMock()
        rc.is_failure = True
        c._on_disconnect(None, None, None, rc)

    @pytest.fixture(autouse=True)
    def _clean_class_state(self):
        BambuMQTTClient._request_topic_cache.pop("TESTSERIAL0001", None)
        BambuMQTTClient._request_topic_strikes.pop("TESTSERIAL0001", None)
        yield
        BambuMQTTClient._request_topic_cache.pop("TESTSERIAL0001", None)
        BambuMQTTClient._request_topic_strikes.pop("TESTSERIAL0001", None)

    def test_one_bad_moment_does_not_convict(self, client):
        self._died_right_after_subscribing(client)

        assert client._request_topic_supported is True
        assert "TESTSERIAL0001" not in BambuMQTTClient._request_topic_cache

    def test_twice_in_a_row_does(self, client):
        self._died_right_after_subscribing(client)
        self._died_right_after_subscribing(client)

        assert client._request_topic_supported is False
        assert BambuMQTTClient._request_topic_cache["TESTSERIAL0001"] is False

    def test_an_accepted_subscription_wipes_the_slate(self, client):
        """⚠️ Strikes are consecutive by design — otherwise a printer that drops
        once a month eventually convicts itself."""
        self._died_right_after_subscribing(client)

        accepted = MagicMock()
        accepted.is_failure = False
        client._request_topic_sub_mid = 7
        client._on_subscribe(None, None, 7, [accepted])

        self._died_right_after_subscribing(client)

        assert client._request_topic_supported is True
