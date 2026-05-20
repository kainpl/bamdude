"""Regression tests for I.1 / upstream Bambuddy #1349.

``BambuMQTTClient._handle_ams_data`` fires ``on_drying_complete(ams_id)``
on the falling edge of an AMS unit's ``dry_time`` (>0 → 0). Per-AMS
state is tracked in ``_previous_dry_times`` so the callback fires once
per cycle and supports multiple AMS units independently.

The plug-side handler (``SmartPlugManager.on_drying_complete``) then
walks every plug linked to that printer and respects the per-plug
``auto_off_after_drying`` toggle — but that's covered by the manager's
own tests; this file pins the MQTT-side edge detection only.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def mqtt_client():
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    events: list[int] = []
    client = BambuMQTTClient(
        ip_address="192.168.1.100",
        serial_number="TEST-DRYING",
        access_code="12345678",
        on_drying_complete=events.append,
    )
    client._drying_events = events  # Expose for assertions
    return client


class TestDryingCompleteCallback:
    def test_falling_edge_fires_callback(self, mqtt_client):
        """First push reports drying active, second reports drying done."""
        mqtt_client._handle_ams_data({"ams": [{"id": "0", "dry_time": 60, "tray": []}]})
        assert mqtt_client._drying_events == []
        mqtt_client._handle_ams_data({"ams": [{"id": "0", "dry_time": 0, "tray": []}]})
        assert mqtt_client._drying_events == [0]

    def test_no_fire_when_dry_time_never_started(self, mqtt_client):
        """``dry_time = 0`` across consecutive pushes does NOT fire —
        there was no drying cycle to finish. Guards against the seed-
        from-zero false positive on startup."""
        mqtt_client._handle_ams_data({"ams": [{"id": "0", "dry_time": 0, "tray": []}]})
        mqtt_client._handle_ams_data({"ams": [{"id": "0", "dry_time": 0, "tray": []}]})
        assert mqtt_client._drying_events == []

    def test_falling_edge_fires_once(self, mqtt_client):
        """Subsequent zero-pushes after the edge don't refire the callback."""
        mqtt_client._handle_ams_data({"ams": [{"id": "0", "dry_time": 30, "tray": []}]})
        mqtt_client._handle_ams_data({"ams": [{"id": "0", "dry_time": 0, "tray": []}]})
        mqtt_client._handle_ams_data({"ams": [{"id": "0", "dry_time": 0, "tray": []}]})
        mqtt_client._handle_ams_data({"ams": [{"id": "0", "dry_time": 0, "tray": []}]})
        assert mqtt_client._drying_events == [0]

    def test_per_ams_tracking(self, mqtt_client):
        """Two AMS units finishing drying at different times each fire
        once — the falling-edge state is keyed per AMS id."""
        mqtt_client._handle_ams_data(
            {"ams": [{"id": "0", "dry_time": 30, "tray": []}, {"id": "1", "dry_time": 30, "tray": []}]}
        )
        mqtt_client._handle_ams_data(
            {"ams": [{"id": "0", "dry_time": 0, "tray": []}, {"id": "1", "dry_time": 15, "tray": []}]}
        )
        assert mqtt_client._drying_events == [0]
        mqtt_client._handle_ams_data(
            {"ams": [{"id": "0", "dry_time": 0, "tray": []}, {"id": "1", "dry_time": 0, "tray": []}]}
        )
        assert mqtt_client._drying_events == [0, 1]

    def test_restart_drying_after_completion_refires_callback(self, mqtt_client):
        """A new drying cycle after the previous one finished fires the
        callback again on its own falling edge — covers the user manually
        starting a second dry from the UI."""
        mqtt_client._handle_ams_data({"ams": [{"id": "0", "dry_time": 30, "tray": []}]})
        mqtt_client._handle_ams_data({"ams": [{"id": "0", "dry_time": 0, "tray": []}]})
        mqtt_client._handle_ams_data({"ams": [{"id": "0", "dry_time": 45, "tray": []}]})
        mqtt_client._handle_ams_data({"ams": [{"id": "0", "dry_time": 0, "tray": []}]})
        assert mqtt_client._drying_events == [0, 0]
