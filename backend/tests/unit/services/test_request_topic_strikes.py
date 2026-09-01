"""Two strikes must be two EPISODES, not two seconds.

The request topic is what captures ``ams_mapping`` for slicer-initiated prints
— priority 1 of the attribution chain, ahead of the live MQTT mapping that an
AMS backup rewrites mid-print. Losing it quietly degrades every such print's
books to the weaker source.

The guard that switches it off exists for printers whose broker really refuses
the subscription, and it already carries a hard-won ⚠️: on 2026-08-21 a single
network event convicted five innocent printers, so one strike was raised to
two. But both strikes still land inside the SAME storm — measured on a live
farm 2026-09-01, every startup produced them 1-2 seconds apart and disabled the
topic across the whole fleet, 70 disables against 14 accepts in one day.

A printer that really refuses does it on every connect, minutes apart. So the
second strike only counts when it is genuinely a second occasion.
"""

import time
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import _REQUEST_TOPIC_STRIKE_GAP, BambuMQTTClient


@pytest.fixture
def client():
    c = BambuMQTTClient(ip_address="192.168.1.60", serial_number="STRIKES01", access_code="12345678")
    BambuMQTTClient._request_topic_strikes.pop("STRIKES01", None)
    BambuMQTTClient._request_topic_strike_at.pop("STRIKES01", None)
    BambuMQTTClient._request_topic_cache.pop("STRIKES01", None)
    c.on_state_change = None
    yield c
    BambuMQTTClient._request_topic_strikes.pop("STRIKES01", None)
    BambuMQTTClient._request_topic_strike_at.pop("STRIKES01", None)
    BambuMQTTClient._request_topic_cache.pop("STRIKES01", None)


def _disconnect_right_after_subscribing(c):
    """One connect that died seconds after the request-topic subscription."""
    c._request_topic_sub_time = time.time()
    c._request_topic_confirmed = False
    c._stale_reconnecting = False
    c._last_message_time = 0.0
    rc = MagicMock()
    rc.is_failure = True
    c._on_disconnect(None, None, None, rc, None)


def test_a_startup_storm_counts_once(client):
    # Every printer reconnects at once and each attempt dies young. That is one
    # event about the network, not two about this printer.
    _disconnect_right_after_subscribing(client)
    _disconnect_right_after_subscribing(client)
    _disconnect_right_after_subscribing(client)

    assert client._request_topic_supported is not False
    assert BambuMQTTClient._request_topic_cache.get("STRIKES01") is not False


def test_a_refusal_on_a_later_occasion_still_latches(client):
    # The case the guard exists for: it happens again, on its own, later.
    _disconnect_right_after_subscribing(client)
    BambuMQTTClient._request_topic_strike_at["STRIKES01"] -= _REQUEST_TOPIC_STRIKE_GAP + 1
    _disconnect_right_after_subscribing(client)

    assert client._request_topic_supported is False
    assert BambuMQTTClient._request_topic_cache["STRIKES01"] is False


def test_an_accepted_subscription_still_clears_the_record(client):
    # Strikes are consecutive by design — a printer that drops once a month must
    # not convict itself. The timestamp has to be forgotten alongside the count,
    # or a single old strike would pair with a new one forever.
    _disconnect_right_after_subscribing(client)
    BambuMQTTClient._request_topic_strikes.pop("STRIKES01", None)
    BambuMQTTClient._request_topic_strike_at.pop("STRIKES01", None)

    _disconnect_right_after_subscribing(client)

    assert client._request_topic_supported is not False
