"""Unit tests for AMS-settings publishers + push-parser additions + hold-timer."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


@pytest.fixture
def mqtt_client():
    client = BambuMQTTClient(
        ip_address="192.168.1.100",
        serial_number="TESTAMS001",
        access_code="12345678",
    )
    # Wire a fake paho client so publish() doesn't NPE; capture its args.
    client._client = MagicMock()
    client.state.connected = True
    return client


def _captured_payload(mqtt_client) -> dict:
    """Pull the JSON string passed to ``_client.publish`` and decode it."""
    call = mqtt_client._client.publish.call_args
    assert call is not None
    _, payload, *_ = call.args
    return json.loads(payload)


# ---------- ams_user_setting ----------


def test_ams_user_setting_payload_shape(mqtt_client):
    ok, seq = mqtt_client.ams_user_setting(startup_read=True, tray_read=False, calibrate_remain=True)
    assert ok is True
    assert seq is not None
    msg = _captured_payload(mqtt_client)
    assert msg["print"]["command"] == "ams_user_setting"
    assert msg["print"]["ams_id"] == -1
    assert msg["print"]["startup_read_option"] is True
    assert msg["print"]["tray_read_option"] is False
    assert msg["print"]["calibrate_remain_flag"] is True
    assert msg["print"]["sequence_id"] == seq


def test_ams_user_setting_stamps_hold_timer(mqtt_client):
    before = time.time()
    mqtt_client.ams_user_setting(startup_read=False, tray_read=False, calibrate_remain=False)
    after = time.time()
    for flag in ("ams_insertion_update", "ams_power_on_update", "ams_remain_capacity"):
        ts = mqtt_client.state.ams_settings_hold.get(flag)
        assert ts is not None, f"hold missing for {flag}"
        assert before <= ts <= after


def test_ams_user_setting_returns_false_when_disconnected(mqtt_client):
    mqtt_client.state.connected = False
    ok, seq = mqtt_client.ams_user_setting(True, True, True)
    assert ok is False
    assert seq is None


# ---------- print_option_auto_switch_filament ----------


def test_print_option_auto_switch_filament_payload(mqtt_client):
    ok, _seq = mqtt_client.print_option_auto_switch_filament(enabled=True)
    assert ok is True
    msg = _captured_payload(mqtt_client)
    assert msg["print"]["command"] == "print_option"
    assert msg["print"]["auto_switch_filament"] is True
    assert "sequence_id" in msg["print"]


def test_print_option_auto_switch_filament_stamps_hold(mqtt_client):
    mqtt_client.print_option_auto_switch_filament(enabled=False)
    assert "ams_auto_switch_filament" in mqtt_client.state.ams_settings_hold


# ---------- print_option_air_print_detect ----------


def test_print_option_air_print_detect_payload(mqtt_client):
    ok, _seq = mqtt_client.print_option_air_print_detect(enabled=False)
    assert ok is True
    msg = _captured_payload(mqtt_client)
    assert msg["print"]["command"] == "print_option"
    assert msg["print"]["air_print_detect"] is False


def test_print_option_air_print_detect_stamps_hold(mqtt_client):
    mqtt_client.print_option_air_print_detect(enabled=True)
    assert "ams_air_print_detect" in mqtt_client.state.ams_settings_hold


# ---------- ams_calibrate (M620 C<id>) ----------


def test_ams_calibrate_sends_gcode_m620(mqtt_client):
    # send_gcode wraps in {"print":{"command":"gcode_line","param":...}}
    ok = mqtt_client.ams_calibrate(ams_id=2)
    assert ok is True
    msg = _captured_payload(mqtt_client)
    assert msg["print"]["command"] == "gcode_line"
    assert "M620 C2" in msg["print"]["param"]


def test_ams_calibrate_when_disconnected_returns_false(mqtt_client):
    mqtt_client.state.connected = False
    assert mqtt_client.ams_calibrate(0) is False


# ---------- push parser: ams.* fields ----------


def test_parser_reads_ams_insert_power_remain(mqtt_client):
    mqtt_client._handle_ams_data(
        {
            "insert_flag": True,
            "power_on_flag": False,
            "calibrate_remain_flag": True,
        }
    )
    assert mqtt_client.state.ams_insertion_update is True
    assert mqtt_client.state.ams_power_on_update is False
    assert mqtt_client.state.ams_remain_capacity is True


def test_parser_respects_active_hold(mqtt_client):
    mqtt_client.state.ams_insertion_update = False
    mqtt_client.state.ams_settings_hold["ams_insertion_update"] = time.time()
    mqtt_client._handle_ams_data({"insert_flag": True})
    # Push value (True) was ignored; previous local value (False) preserved.
    assert mqtt_client.state.ams_insertion_update is False


def test_parser_releases_hold_after_3s(mqtt_client):
    mqtt_client.state.ams_insertion_update = False
    mqtt_client.state.ams_settings_hold["ams_insertion_update"] = time.time() - 5.0
    mqtt_client._handle_ams_data({"insert_flag": True})
    assert mqtt_client.state.ams_insertion_update is True


# ---------- push parser: print_option echoes ----------


def test_parser_reads_auto_switch_filament_echo(mqtt_client):
    # Simulate the print-level handler by calling the same code path it uses.
    # The echo lives directly on print_data, not under ams.
    msg = {"print": {"auto_switch_filament": True, "command": "push_status"}}
    # Route through the public message handler so we exercise the real branch.
    mqtt_client._on_message(None, None, _FakeMQTTMsg(msg))
    assert mqtt_client.state.ams_auto_switch_filament is True


def test_parser_reads_air_print_detect_echo(mqtt_client):
    msg = {"print": {"air_print_detect": False, "command": "push_status"}}
    mqtt_client._on_message(None, None, _FakeMQTTMsg(msg))
    assert mqtt_client.state.ams_air_print_detect is False


# ---------- push parser: print.cfg hex bitfield (newer firmware) ----------


def test_parser_reads_all_four_flags_from_cfg_hex_string(mqtt_client):
    # bit 0 = insert, bit 1 = power_on, bit 17 = remain, bit 18 = auto_switch.
    # 0x60003 → bits 0, 1, 17, 18 → all True.
    msg = {"print": {"cfg": "0x60003", "command": "push_status"}}
    mqtt_client._on_message(None, None, _FakeMQTTMsg(msg))
    assert mqtt_client.state.ams_insertion_update is True
    assert mqtt_client.state.ams_power_on_update is True
    assert mqtt_client.state.ams_remain_capacity is True
    assert mqtt_client.state.ams_auto_switch_filament is True


def test_parser_cfg_only_auto_switch_set(mqtt_client):
    # bit 18 only → 0x40000.
    msg = {"print": {"cfg": "40000", "command": "push_status"}}
    mqtt_client._on_message(None, None, _FakeMQTTMsg(msg))
    assert mqtt_client.state.ams_insertion_update is False
    assert mqtt_client.state.ams_power_on_update is False
    assert mqtt_client.state.ams_remain_capacity is False
    assert mqtt_client.state.ams_auto_switch_filament is True


def test_parser_cfg_respects_hold_for_auto_switch(mqtt_client):
    # Hold-stamp the flag — its previous True value must survive an incoming
    # cfg with bit 18 cleared.
    mqtt_client.state.ams_auto_switch_filament = True
    mqtt_client.state.ams_settings_hold["ams_auto_switch_filament"] = time.time()
    msg = {"print": {"cfg": "0", "command": "push_status"}}
    mqtt_client._on_message(None, None, _FakeMQTTMsg(msg))
    assert mqtt_client.state.ams_auto_switch_filament is True


def test_parser_cfg_invalid_hex_does_not_crash(mqtt_client):
    msg = {"print": {"cfg": "not-hex", "command": "push_status"}}
    mqtt_client._on_message(None, None, _FakeMQTTMsg(msg))
    # No exception; flags remain None.
    assert mqtt_client.state.ams_auto_switch_filament is None


# ---------- push parser: home_flag bits (X1 / P1 path) ----------


def test_parser_reads_remain_and_backup_from_home_flag(mqtt_client):
    # bit 7 = remain, bit 10 = auto_refill (filament backup).
    # 1<<7 | 1<<10 = 0x480 = 1152
    msg = {"print": {"home_flag": 1152, "command": "push_status"}}
    mqtt_client._on_message(None, None, _FakeMQTTMsg(msg))
    assert mqtt_client.state.ams_remain_capacity is True
    assert mqtt_client.state.ams_auto_switch_filament is True


def test_parser_home_flag_only_backup(mqtt_client):
    msg = {"print": {"home_flag": 1 << 10, "command": "push_status"}}
    mqtt_client._on_message(None, None, _FakeMQTTMsg(msg))
    assert mqtt_client.state.ams_remain_capacity is False
    assert mqtt_client.state.ams_auto_switch_filament is True


def test_parser_home_flag_negative_int_handled(mqtt_client):
    # Bambu sometimes sends signed-int home_flag; parser must masquerade
    # to unsigned 32-bit before reading bits.
    raw = -(1 << 31) | (1 << 10)  # high bit set + bit 10
    msg = {"print": {"home_flag": raw, "command": "push_status"}}
    mqtt_client._on_message(None, None, _FakeMQTTMsg(msg))
    assert mqtt_client.state.ams_auto_switch_filament is True


def test_parser_home_flag_respects_hold(mqtt_client):
    mqtt_client.state.ams_auto_switch_filament = True
    mqtt_client.state.ams_settings_hold["ams_auto_switch_filament"] = time.time()
    msg = {"print": {"home_flag": 0, "command": "push_status"}}
    mqtt_client._on_message(None, None, _FakeMQTTMsg(msg))
    assert mqtt_client.state.ams_auto_switch_filament is True


class _FakeMQTTMsg:
    """Minimal stand-in for paho.MQTTMessage."""

    def __init__(self, payload_dict):
        import json as _json

        self.topic = ""
        self.payload = _json.dumps(payload_dict).encode("utf-8")


# ---------- ams_firmware_switch ----------


def test_ams_firmware_switch_payload(mqtt_client):
    ok, seq = mqtt_client.ams_firmware_switch(firmware_idx=1)
    assert ok is True
    assert seq is not None
    msg = _captured_payload(mqtt_client)
    # NB: under "upgrade", not "print" — confirmed from DevFilaAmsSettingCtrl.cpp:7.
    assert msg["upgrade"]["command"] == "mc_for_ams_firmware_upgrade"
    assert msg["upgrade"]["src_id"] == 1
    assert msg["upgrade"]["id"] == 1
    assert msg["upgrade"]["sequence_id"] == seq


# ---------- ams_reset_sequence (BS ams_reset) ----------


def test_ams_reset_sequence_payload(mqtt_client):
    ok, seq = mqtt_client.ams_reset_sequence()
    assert ok is True
    assert seq is not None
    msg = _captured_payload(mqtt_client)
    assert msg["print"]["command"] == "ams_reset"
    # No order field — BS doesn't send one.
    assert "order" not in msg["print"]
