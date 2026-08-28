"""The tray-change log has to leave the process.

``PrinterState.tray_change_log`` is what the usage tracker splits filament
weight on when AMS filament backup swaps in a fresh spool mid-print. It lived
only in memory, so a restart during a long print erased the segment boundaries
and the whole job was charged to the tray that happened to finish it. The client
now reports every appended entry so main.py can persist it (upstream
`454457a0`).

⚠️ The callback is a *mirror* of the append, not a second decision: anything
that is logged is reported, and nothing else. Two gates that could drift apart
would be worse than one, because the persisted log is only consulted after a
restart — when there is nothing left to compare it against.
"""

from __future__ import annotations

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _tray_msg(tray_now: int) -> dict:
    """A partial AMS update carrying only tray_now, as P-series and H2D send."""
    return {"print": {"ams": {"tray_now": str(tray_now)}}}


class TestTrayChangeCallback:
    @pytest.fixture
    def mqtt_client(self):
        client = BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="TEST123",
            access_code="12345678",
        )
        # The append is gated on the print-lifecycle flags rather than on the
        # literal state string — P2S briefly leaves RUNNING during an AMS
        # auto-fallback, which is the very switch we must not miss (#957).
        client._was_running = True
        client._completion_triggered = False
        return client

    def test_every_logged_change_is_reported(self, mqtt_client):
        seen: list[tuple[int, int]] = []
        mqtt_client.on_tray_change = lambda tray, layer: seen.append((tray, layer))

        mqtt_client.state.layer_num = 0
        mqtt_client._process_message(_tray_msg(2))
        mqtt_client.state.layer_num = 670
        mqtt_client._process_message(_tray_msg(254))
        mqtt_client.state.layer_num = 675
        mqtt_client._process_message(_tray_msg(3))

        assert seen == [(2, 0), (254, 670), (3, 675)]
        assert mqtt_client.state.tray_change_log == [(2, 0), (254, 670), (3, 675)]

    def test_a_repeat_of_the_same_tray_is_not_reported(self, mqtt_client):
        """The printer republishes tray_now on every push; only transitions are
        segment boundaries."""
        seen: list[tuple[int, int]] = []
        mqtt_client.on_tray_change = lambda tray, layer: seen.append((tray, layer))

        mqtt_client._process_message(_tray_msg(2))
        mqtt_client.state.layer_num = 40
        mqtt_client._process_message(_tray_msg(2))

        assert seen == [(2, 0)]

    def test_nothing_is_reported_outside_a_running_print(self, mqtt_client):
        seen: list[tuple[int, int]] = []
        mqtt_client.on_tray_change = lambda tray, layer: seen.append((tray, layer))
        mqtt_client._was_running = False

        mqtt_client._process_message(_tray_msg(2))

        assert seen == []
        assert mqtt_client.state.tray_change_log == []

    def test_a_missing_callback_does_not_break_the_logging(self, mqtt_client):
        """The callback is optional — the in-memory log still has to work."""
        mqtt_client.on_tray_change = None

        mqtt_client._process_message(_tray_msg(2))

        assert mqtt_client.state.tray_change_log == [(2, 0)]


class TestTheWayThePrinterManagerWiresIt:
    def test_the_constructor_takes_it_by_the_name_the_manager_passes(self):
        """``PrinterManager`` hands the callback in as a keyword argument when it
        builds the client. A rename on either side is a TypeError at connect
        time — on a live printer, mid-print, where nothing else would catch it."""
        seen: list[tuple[int, int]] = []
        client = BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="TEST123",
            access_code="12345678",
            on_tray_change=lambda tray, layer: seen.append((tray, layer)),
        )
        client._was_running = True
        client._completion_triggered = False

        client._process_message(_tray_msg(2))

        assert seen == [(2, 0)]
