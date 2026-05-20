"""Regression tests for G.7 / upstream Bambuddy #1279.

``ams_set_filament_setting`` and ``reset_ams_slot`` build their MQTT
payload from ``ams_id`` + ``tray_id``. For external-spool slots
(``ams_id=255``), Bambu firmware splits the encoding into ``mqtt_ams_id``
(virtual AMS id, ``254`` for left / 255 for main) and ``mqtt_tray_id``
(global tray index, 254/255). The previous code sent ``mqtt_tray_id = 0``
under the false assumption that the field was a local slot position
within the virtual unit — which the P1S in #1279 rejected with
``result: "fail"``.

A BambuStudio → X1C packet capture verified the on-wire shape is
``{ams_id: 255, tray_id: 254, slot_id: 0}`` for the single-external
case. These tests pin that encoding so a future "clean up the magic
numbers" refactor can't silently regress filament selection for the
no-AMS / external-spool fleet.

Dual-external (H2D, ``len(vt_tray) > 1``) is NOT in the verified
capture and is intentionally left at ``mqtt_tray_id = 0`` — pinned
separately so any future change to that branch surfaces in diff
review.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def client():
    from backend.app.services.bambu_mqtt import BambuMQTTClient, PrinterState

    c = BambuMQTTClient(ip_address="192.168.1.50", serial_number="01S00", access_code="11111111")
    c._client = MagicMock()  # bypass paho client; we only assert payloads
    c.state = PrinterState()
    c.state.connected = True
    return c


def _capture_payload(client):
    """Return the last MQTT payload published by ``client._client.publish``
    decoded as a dict (the client always passes JSON)."""
    import json

    args = client._client.publish.call_args
    return json.loads(args.args[1] if args.args else args.kwargs["payload"])


class TestSingleExternalSlot:
    """Reporter scenario: P1S, no AMS, single ``vt_tray`` entry. The fix
    sets ``mqtt_tray_id = 254`` so the printer accepts the request."""

    def test_set_filament_setting_uses_global_tray_id_254(self, client):
        # Single external slot — printer's push reports vt_tray as a
        # one-element list.
        client.state.raw_data = {"vt_tray": [{"id": 254}]}

        client.ams_set_filament_setting(
            ams_id=255,
            tray_id=0,
            tray_info_idx="P4d64437",
            tray_type="PLA",
            tray_sub_brands="PLA Basic",
            tray_color="F72323FF",
            nozzle_temp_min=190,
            nozzle_temp_max=230,
            setting_id="PFUS00000001",
        )

        payload = _capture_payload(client)
        cmd = payload["print"]
        assert cmd["ams_id"] == 255, "external virtual AMS id stays at 255 for single-ext"
        assert cmd["tray_id"] == 254, "global tray_id is the #1279 fix"
        assert cmd["slot_id"] == 0, "slot_id stays 0 for the single virtual slot"

    def test_reset_ams_slot_uses_global_tray_id_254(self, client):
        """``reset_ams_slot`` shares the convention — same fix applies."""
        client.state.raw_data = {"vt_tray": [{"id": 254}]}

        client.reset_ams_slot(ams_id=255, tray_id=0)

        payload = _capture_payload(client)
        cmd = payload["print"]
        assert cmd["ams_id"] == 255
        assert cmd["tray_id"] == 254
        assert cmd["slot_id"] == 0


class TestDualExternalSlot:
    """H2D / H2C / H2S have two external slots reported as a 2-element
    ``vt_tray``. That branch was NOT in the X1C capture; intentionally
    left at the legacy ``mqtt_tray_id = 0`` until a Studio → H2D capture
    confirms the correct value. Pinned here so the dual-external code
    path is impossible to silently change."""

    def test_dual_external_left_slot_keeps_legacy_zero_encoding(self, client):
        client.state.raw_data = {"vt_tray": [{"id": 254}, {"id": 255}]}

        client.ams_set_filament_setting(
            ams_id=255,
            tray_id=0,  # left ext-L
            tray_info_idx="P4d64437",
            tray_type="PLA",
            tray_sub_brands="PLA Basic",
            tray_color="F72323FF",
            nozzle_temp_min=190,
            nozzle_temp_max=230,
            setting_id="PFUS00000001",
        )

        payload = _capture_payload(client)
        cmd = payload["print"]
        assert cmd["ams_id"] == 254, "dual-ext left → virtual AMS 254"
        assert cmd["tray_id"] == 0, "dual-external pinned at legacy 0 pending H2D capture"

    def test_dual_external_right_slot_keeps_legacy_zero_encoding(self, client):
        client.state.raw_data = {"vt_tray": [{"id": 254}, {"id": 255}]}

        client.ams_set_filament_setting(
            ams_id=255,
            tray_id=1,  # right ext-R
            tray_info_idx="P4d64437",
            tray_type="PLA",
            tray_sub_brands="PLA Basic",
            tray_color="F72323FF",
            nozzle_temp_min=190,
            nozzle_temp_max=230,
            setting_id="PFUS00000001",
        )

        payload = _capture_payload(client)
        cmd = payload["print"]
        assert cmd["ams_id"] == 255, "dual-ext right → virtual AMS 255"
        assert cmd["tray_id"] == 0


class TestRegularAmsSlotUnchanged:
    """Sanity guards: the #1279 change must NOT affect regular AMS slots
    (``ams_id 0..3``) or AMS-HT (``128..135``)."""

    def test_regular_ams_slot_unchanged(self, client):
        client.ams_set_filament_setting(
            ams_id=0,
            tray_id=2,
            tray_info_idx="P4d64437",
            tray_type="PLA",
            tray_sub_brands="PLA Basic",
            tray_color="F72323FF",
            nozzle_temp_min=190,
            nozzle_temp_max=230,
            setting_id="PFUS00000001",
        )

        payload = _capture_payload(client)
        cmd = payload["print"]
        assert cmd["ams_id"] == 0
        assert cmd["tray_id"] == 2
        assert cmd["slot_id"] == 2

    def test_ams_ht_slot_unchanged(self, client):
        client.ams_set_filament_setting(
            ams_id=128,
            tray_id=0,
            tray_info_idx="P4d64437",
            tray_type="PLA",
            tray_sub_brands="PLA Basic",
            tray_color="F72323FF",
            nozzle_temp_min=190,
            nozzle_temp_max=230,
            setting_id="PFUS00000001",
        )

        payload = _capture_payload(client)
        cmd = payload["print"]
        assert cmd["ams_id"] == 128
        # AMS-HT: single tray per unit; we don't assert on the exact
        # mqtt_tray_id encoding for HT here — that's covered by the
        # AMS-HT range-test suite (#1274 / D.4).
