"""Unit tests for Printer Settings dialog publishers + hold-timer."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


@pytest.fixture
def mqtt_client():
    c = BambuMQTTClient(
        ip_address="192.168.1.100",
        serial_number="TESTPS001",
        access_code="12345678",
    )
    c._client = MagicMock()
    c.state.connected = True
    return c


def _payload(c) -> dict:
    call = c._client.publish.call_args
    _, payload, *_ = call.args
    return json.loads(payload)


# ---------- Bool toggles via print.command="print_option" ----------


@pytest.mark.parametrize(
    "method,field",
    [
        ("print_option_auto_recovery", "auto_recovery"),
        ("print_option_sound", "sound_enable"),
        ("print_option_filament_tangle", "filament_tangle_detect"),
        ("print_option_nozzle_blob", "nozzle_blob_detect"),
        ("print_option_plate_type", "build_plate_marker_detect"),
        ("print_option_plate_align", "plate_align_check"),
    ],
)
def test_print_option_bool_payload(mqtt_client, method, field):
    ok, seq = getattr(mqtt_client, method)(True)
    assert ok is True and seq is not None
    msg = _payload(mqtt_client)
    assert msg["print"]["command"] == "print_option"
    assert msg["print"][field] is True
    assert msg["print"]["sequence_id"] == seq


def test_print_option_bool_stamps_hold(mqtt_client):
    before = time.time()
    mqtt_client.print_option_auto_recovery(True)
    after = time.time()
    ts = mqtt_client.state.printer_settings_hold.get("auto_recovery")
    assert ts is not None and before <= ts <= after


def test_print_option_returns_false_when_disconnected(mqtt_client):
    mqtt_client.state.connected = False
    ok, seq = mqtt_client.print_option_auto_recovery(True)
    assert ok is False and seq is None


# ---------- Int toggles via print.command="print_option" ----------


@pytest.mark.parametrize(
    "method,field,value",
    [
        ("print_option_purify_air", "air_purification", 2),
        ("print_option_open_door", "xcam_door_open_check", 1),
        ("print_option_save_remote_to_storage", "xcam__save_remote_print_file_to_storage", 1),
    ],
)
def test_print_option_int_payload(mqtt_client, method, field, value):
    ok, seq = getattr(mqtt_client, method)(value)
    assert ok is True and seq is not None
    msg = _payload(mqtt_client)
    assert msg["print"]["command"] == "print_option"
    assert msg["print"][field] == value


# ---------- Camera snapshot ----------


def test_camera_snapshot_enable_payload(mqtt_client):
    ok, seq = mqtt_client.camera_snapshot_enable(True)
    assert ok is True and seq is not None
    msg = _payload(mqtt_client)
    assert msg["camera"]["command"] == "ipcam_cap_pic_set"
    assert msg["camera"]["control"] == "enable"


def test_camera_snapshot_disable_payload(mqtt_client):
    ok, _ = mqtt_client.camera_snapshot_enable(False)
    assert ok is True
    msg = _payload(mqtt_client)
    assert msg["camera"]["control"] == "disable"


def test_camera_snapshot_stamps_hold(mqtt_client):
    mqtt_client.camera_snapshot_enable(True)
    assert "snapshot" in mqtt_client.state.printer_settings_hold


def test_xcam_control_wrapper_returns_seq_and_stamps_hold(mqtt_client):
    ok, seq = mqtt_client.xcam_control_for_settings("spaghetti_detector", enabled=True, sensitivity="high")
    assert ok is True and seq is not None
    msg = _payload(mqtt_client)
    assert msg["xcam"]["command"] == "xcam_control_set"
    assert msg["xcam"]["module_name"] == "spaghetti_detector"
    assert msg["xcam"]["control"] is True
    assert msg["xcam"]["halt_print_sensitivity"] == "high"
    assert "spaghetti_detector" in mqtt_client.state.printer_settings_hold


def test_xcam_control_wrapper_sensitivity_optional(mqtt_client):
    ok, _ = mqtt_client.xcam_control_for_settings("fod_check", enabled=False, sensitivity=None)
    assert ok is True
    msg = _payload(mqtt_client)
    assert msg["xcam"]["module_name"] == "fod_check"
    assert msg["xcam"]["control"] is False
    assert "halt_print_sensitivity" not in msg["xcam"]


# ---------- Parser: print.print_option echoes ----------


class _FakeMQTTMsg:
    def __init__(self, payload_dict):
        import json as _json

        self.topic = ""
        self.payload = _json.dumps(payload_dict).encode("utf-8")


def test_parser_reads_print_option_bool_echoes(mqtt_client):
    msg = {
        "print": {
            "command": "push_status",
            "auto_recovery": True,
            "sound_enable": False,
            "filament_tangle_detect": True,
            "nozzle_blob_detect": True,
            "build_plate_marker_detect": False,
            "plate_align_check": True,
        }
    }
    mqtt_client._on_message(None, None, _FakeMQTTMsg(msg))
    po = mqtt_client.state.print_options
    assert po.auto_recovery_step_loss is True
    assert po.sound_enable is False
    assert po.filament_tangle_detect is True
    assert po.nozzle_blob_detect is True
    assert po.plate_type_detect is False
    assert po.plate_align_check is True


def test_parser_reads_print_option_int_echoes(mqtt_client):
    msg = {
        "print": {
            "command": "push_status",
            "air_purification": 2,
            "xcam_door_open_check": 1,
            "xcam__save_remote_print_file_to_storage": 0,
        }
    }
    mqtt_client._on_message(None, None, _FakeMQTTMsg(msg))
    po = mqtt_client.state.print_options
    assert po.air_purification == 2
    assert po.open_door_check == 1
    assert po.save_remote_to_storage == 0


def test_parser_respects_printer_settings_hold(mqtt_client):
    mqtt_client.state.print_options.auto_recovery_step_loss = False
    mqtt_client.state.printer_settings_hold["auto_recovery"] = time.time()
    msg = {"print": {"command": "push_status", "auto_recovery": True}}
    mqtt_client._on_message(None, None, _FakeMQTTMsg(msg))
    assert mqtt_client.state.print_options.auto_recovery_step_loss is False
