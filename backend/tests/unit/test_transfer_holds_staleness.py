"""A printer that goes quiet while it swallows a file is not a stale session.

2026-09-24: two A1 mini went silent on MQTT for 64–99 s during a 100 s upload;
the stale detector reconnected both, the feed cache emptied with the new
generation, and both prints were refused at the final guard (vault
a1-mini-reprint-silently-cancelled-2026-09-25). Spec direct-print-silent-cancel §4.2.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.printer_manager import printer_manager


def _client() -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TESTSERIAL", access_code="12345678", model="A1 Mini")
    c._client = MagicMock()
    c.state.connected = True
    return c


def test_silence_during_a_transfer_is_not_staleness():
    c = _client()
    c._last_message_time = time.time() - 10 * c.STALE_TIMEOUT
    c.begin_transfer()
    assert c.is_stale() is False
    assert c.check_staleness() is True and c.state.connected is True


def test_silence_is_counted_from_the_end_of_the_transfer():
    c = _client()
    c._last_message_time = time.time() - 10 * c.STALE_TIMEOUT
    c.begin_transfer()
    c.end_transfer()
    assert c.is_stale() is False, "the printer gets a full timeout to speak after the file lands"
    c._transfer_ended_at = time.time() - c.STALE_TIMEOUT - 1
    assert c.is_stale() is True


def test_a_message_after_the_transfer_resets_the_count():
    c = _client()
    c.begin_transfer()
    c.end_transfer()
    c._transfer_ended_at = time.time() - 10 * c.STALE_TIMEOUT
    c._last_message_time = time.time()
    assert c.is_stale() is False


def test_two_transfers_hold_until_both_end():
    c = _client()
    c._last_message_time = time.time() - 10 * c.STALE_TIMEOUT
    c.begin_transfer()
    c.begin_transfer()
    c.end_transfer()
    assert c.is_stale() is False
    c.end_transfer()
    c.end_transfer()  # an unmatched end never goes below zero
    assert c._transfers_active == 0


def test_the_manager_releases_the_client_that_took_the_hold(monkeypatch):
    first, second = MagicMock(), MagicMock()
    monkeypatch.setitem(printer_manager._clients, 4242, first)
    with printer_manager.transfer_in_progress(4242):
        # connection_watchdog rebuilt the session mid-transfer
        monkeypatch.setitem(printer_manager._clients, 4242, second)
    first.begin_transfer.assert_called_once()
    first.end_transfer.assert_called_once()
    second.end_transfer.assert_not_called()


def test_the_hold_is_released_when_the_transfer_raises(monkeypatch):
    client = MagicMock()
    monkeypatch.setitem(printer_manager._clients, 4243, client)
    with pytest.raises(OSError), printer_manager.transfer_in_progress(4243):
        raise OSError("FTP upload failed")
    client.end_transfer.assert_called_once()


def test_no_client_is_a_no_op():
    with printer_manager.transfer_in_progress(999_999):
        pass


def test_a_hold_older_than_its_cap_stops_hiding_silence():
    """A transport that hangs rather than crawls must not switch the detector off for ever."""
    c = _client()
    c._last_message_time = time.time() - 10 * c.STALE_TIMEOUT
    c.begin_transfer()
    c._transfer_started_at = time.time() - c.TRANSFER_HOLD_MAX - 1
    assert c.is_stale() is True


def test_silent_since_the_transfer():
    c = _client()
    assert c.silent_since_transfer() is False, "no transfer, nothing to be silent since"
    c._last_message_time = time.time() - 5
    c.begin_transfer()
    c.end_transfer()
    assert c.silent_since_transfer() is True
    c._last_message_time = time.time() + 1
    assert c.silent_since_transfer() is False


@pytest.mark.asyncio
async def test_confirming_a_printer_that_spoke_after_the_upload_asks_nothing(monkeypatch):
    c = _client()
    c.begin_transfer()
    c.end_transfer()
    c._last_message_time = time.time() + 1
    c.request_status_update = MagicMock()
    monkeypatch.setitem(printer_manager._clients, 4244, c)
    assert await printer_manager.confirm_heard_after_transfer(4244, timeout=0.05, poll=0.01) is True
    c.request_status_update.assert_not_called()


@pytest.mark.asyncio
async def test_a_silent_printer_is_asked_and_confirmed_when_it_answers(monkeypatch):
    c = _client()
    c._last_message_time = time.time() - 5
    c.begin_transfer()
    c.end_transfer()

    def answer():
        c._last_message_time = time.time() + 1

    c.request_status_update = MagicMock(side_effect=answer)
    monkeypatch.setitem(printer_manager._clients, 4245, c)
    assert await printer_manager.confirm_heard_after_transfer(4245, timeout=1, poll=0.01) is True
    c.request_status_update.assert_called_once()


@pytest.mark.asyncio
async def test_a_printer_that_stays_silent_is_not_confirmed(monkeypatch):
    c = _client()
    c._last_message_time = time.time() - 5
    c.begin_transfer()
    c.end_transfer()
    c.request_status_update = MagicMock()
    monkeypatch.setitem(printer_manager._clients, 4246, c)
    assert await printer_manager.confirm_heard_after_transfer(4246, timeout=0.05, poll=0.01) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("heard,reconnects", [(True, False), (False, True)])
async def test_the_pre_start_check_reconnects_a_session_silent_since_the_upload(monkeypatch, heard, reconnects):
    """The post-transfer grace keeps ``is_connected`` true for a minute; a session
    that died during the upload would otherwise take the start command into the
    void. Asked, and reconnected when it does not answer (final review, Minor 6)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from backend.app.services.background_dispatch import BackgroundDispatchService

    monkeypatch.setattr(printer_manager, "is_connected", lambda pid: True)
    monkeypatch.setattr(printer_manager, "confirm_heard_after_transfer", AsyncMock(return_value=heard))
    connect = AsyncMock(return_value=True)
    monkeypatch.setattr(printer_manager, "connect_printer", connect)
    await BackgroundDispatchService._ensure_live_connection_before_start(SimpleNamespace(id=4247), "pesduke")
    assert connect.await_count == (1 if reconnects else 0)
