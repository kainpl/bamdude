"""A printer's refusal of the request-topic subscription is remembered across restarts, per firmware.

Some printers answer the subscription to their request topic by dropping the connection. The verdict
lived in process memory only, so every start probed again: two dropped connections and some four
seconds per printer, on every start (farm log, 2026-11-18: the same five printers, each start). The
verdict is now kept on the printer's row together with the firmware it was reached on
(``printers.request_topic_refused_firmware``); a printer that reports another firmware is probed again.
The firmware is only known after a connection lives, so the stored value is confirmed -- or forgotten --
when the version report arrives, never before the connect.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from backend.app.models.printer import Printer
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.printer_manager import store_request_topic_verdict

SERIAL = "VERDICT00000001"


@pytest.fixture(autouse=True)
def _clean_class_state():
    BambuMQTTClient._request_topic_cache.clear()
    BambuMQTTClient._request_topic_strikes.clear()
    yield
    BambuMQTTClient._request_topic_cache.clear()
    BambuMQTTClient._request_topic_strikes.clear()


def _client(stored=None):
    verdicts: list[str | None] = []
    client = BambuMQTTClient(
        ip_address="192.0.2.10",
        access_code="fixture",
        serial_number=SERIAL,
        request_topic_refused_firmware=stored,
        on_request_topic_verdict=verdicts.append,
    )
    return client, verdicts


async def _stored(db) -> dict:
    return dict((await db.execute(select(Printer.id, Printer.request_topic_refused_firmware))).all())


def _version(client, firmware):
    client._handle_version_info({"command": "get_version", "module": [{"name": "ota", "sw_ver": firmware}]})


def test_a_stored_refusal_skips_the_subscription():
    client, _ = _client(stored="01.08.01.00")
    paho = MagicMock()
    paho.subscribe.return_value = (0, 1)

    assert client._request_topic_supported is False
    client._on_connect(paho, None, None, 0)

    assert [call.args[0] for call in paho.subscribe.call_args_list] == [client.topic_subscribe]


def test_the_same_firmware_keeps_the_stored_refusal():
    client, verdicts = _client(stored="01.08.01.00")

    _version(client, "01.08.01.00")

    assert verdicts == []
    assert client._request_topic_supported is False


def test_another_firmware_forgets_it_and_the_next_connect_probes_again():
    client, verdicts = _client(stored="01.08.01.00")

    _version(client, "01.09.00.00")

    assert verdicts == [None]
    assert client._request_topic_supported is True
    assert SERIAL not in BambuMQTTClient._request_topic_cache
    # A new client of the same printer (a reconnect) probes, although its row object may be stale.
    again, _ = _client(stored=None)
    assert again._request_topic_supported is True


def test_a_refusal_met_in_this_process_is_reported_with_its_firmware_once():
    client, verdicts = _client()
    client._request_topic_supported = False
    BambuMQTTClient._request_topic_cache[SERIAL] = False

    _version(client, "01.08.01.00")
    _version(client, "01.08.01.00")

    assert verdicts == ["01.08.01.00"]


def test_a_printer_that_accepts_reports_nothing():
    client, verdicts = _client()
    BambuMQTTClient._request_topic_cache[SERIAL] = True

    _version(client, "01.03.00.00")

    assert verdicts == []


def test_a_refusal_decided_after_the_version_is_known_is_reported_at_once():
    """A printer whose version arrived first, then dropped twice on the subscription."""
    client, verdicts = _client()
    _version(client, "01.08.01.00")
    assert verdicts == []

    client._refuse_request_topic()

    assert verdicts == ["01.08.01.00"]


async def test_the_verdict_is_written_to_the_printers_own_row(db_session, printer_factory):
    # The writer commits, which expires the session's rows: keep the ids.
    printer_id = (await printer_factory(serial_number=SERIAL)).id
    other_id = (await printer_factory()).id

    await store_request_topic_verdict(db_session, printer_id, SERIAL, "01.08.01.00")

    assert await _stored(db_session) == {printer_id: "01.08.01.00", other_id: None}

    await store_request_topic_verdict(db_session, printer_id, SERIAL, None)

    assert await _stored(db_session) == {printer_id: None, other_id: None}


async def test_a_verdict_never_lands_on_another_printer_with_the_same_id(db_session, printer_factory):
    """SQLite reuses ids: the row is addressed by its id AND its serial number."""
    printer_id = (await printer_factory(serial_number="SOMEONE00000001")).id

    await store_request_topic_verdict(db_session, printer_id, SERIAL, "01.08.01.00")

    assert await _stored(db_session) == {printer_id: None}
