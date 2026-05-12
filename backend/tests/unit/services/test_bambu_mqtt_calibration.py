"""Tests calibration MQTT publishers + push parsers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


@pytest.fixture
def mqtt_client():
    c = BambuMQTTClient(
        ip_address="192.168.1.100",
        serial_number="TESTCALI01",
        access_code="12345678",
    )
    c._client = MagicMock()
    c.state.connected = True
    return c


def _last_payload(c) -> dict:
    """Decode the last publish() call's JSON body."""
    args, _kwargs = c._client.publish.call_args
    # Existing BambuMQTTClient publish signature: (topic, json_str, qos=1)
    return json.loads(args[1])


# ---------- Publishers ----------


def test_extrusion_cali_start_payload(mqtt_client):
    ok, seq = mqtt_client.extrusion_cali_start(
        nozzle_diameter=0.4,
        cali_mode=1,
        filaments=[
            {
                "tray_id": 0,
                "extruder_id": 0,
                "bed_temp": 60,
                "filament_id": "GFG00",
                "setting_id": "GFG00_60@BBL",
                "nozzle_temp": 220,
                "ams_id": 0,
                "slot_id": 0,
                "nozzle_id": "HS20",
                "nozzle_diameter": "0.4",
                "max_volumetric_speed": "12.0",
            }
        ],
    )
    assert ok and seq
    msg = _last_payload(mqtt_client)
    assert msg["print"]["command"] == "extrusion_cali"
    assert msg["print"]["nozzle_diameter"] == "0.4"
    assert msg["print"]["mode"] == 1
    assert msg["print"]["filaments"][0]["filament_id"] == "GFG00"
    # State should track active session
    assert mqtt_client.state.extrusion_cali_session_id == seq
    assert mqtt_client.state.extrusion_cali_status == "running"


def test_flow_rate_cali_start_payload(mqtt_client):
    ok, seq = mqtt_client.flow_rate_cali_start(
        nozzle_diameter=0.4,
        filaments=[
            {
                "tray_id": 0,
                "extruder_id": 0,
                "bed_temp": 60,
                "filament_id": "GFG00",
                "setting_id": "GFG00_60@BBL",
                "nozzle_temp": 220,
                "ams_id": 0,
                "slot_id": 0,
                "nozzle_id": "HS20",
                "nozzle_diameter": "0.4",
                "max_volumetric_speed": "12.0",
                "flow_rate": 0.98,
            }
        ],
    )
    assert ok and seq
    msg = _last_payload(mqtt_client)
    assert msg["print"]["command"] == "extrusion_cali"
    assert msg["print"]["filaments"][0]["flow_rate"] == 0.98


def test_extrusion_cali_query_history(mqtt_client):
    ok, seq = mqtt_client.extrusion_cali_query_history(nozzle_diameter=0.4, extruder_id=0)
    assert ok and seq
    msg = _last_payload(mqtt_client)
    assert msg["print"]["command"] == "extrusion_cali_get"


def test_extrusion_cali_query_result(mqtt_client):
    ok, seq = mqtt_client.extrusion_cali_query_result(nozzle_diameter=0.4)
    assert ok and seq
    msg = _last_payload(mqtt_client)
    assert msg["print"]["command"] == "extrusion_cali_get_result"


def test_publishers_return_false_when_disconnected(mqtt_client):
    mqtt_client.state.connected = False
    assert mqtt_client.extrusion_cali_start(nozzle_diameter=0.4, cali_mode=0, filaments=[]) == (False, None)
    assert mqtt_client.flow_rate_cali_start(nozzle_diameter=0.4, filaments=[]) == (False, None)
    assert mqtt_client.extrusion_cali_query_history(nozzle_diameter=0.4) == (False, None)
    assert mqtt_client.extrusion_cali_query_result(nozzle_diameter=0.4) == (False, None)


# ---------- Push parser: extrusion_cali_get_result ----------


class _FakeMsg:
    def __init__(self, body: dict):
        self.topic = ""
        self.payload = json.dumps(body).encode()


def test_parser_extrusion_cali_get_result_populates_state(mqtt_client):
    # Simulate ongoing session
    mqtt_client.state.extrusion_cali_status = "running"
    msg = {
        "print": {
            "command": "extrusion_cali_get_result",
            "filaments": [
                {
                    "tray_id": 0,
                    "ams_id": 0,
                    "slot_id": 0,
                    "extruder_id": 0,
                    "nozzle_diameter": 0.4,
                    "nozzle_volume_type": "standard",
                    "filament_id": "GFG00",
                    "setting_id": "GFG00_60@BBL",
                    "k_value": 0.0432,
                    "n_coef": 1.0,
                    "confidence": 0,
                }
            ],
        }
    }
    mqtt_client._on_message(None, None, _FakeMsg(msg))
    results = mqtt_client.state.extrusion_cali_results
    assert len(results) == 1
    assert abs(results[0].k_value - 0.0432) < 1e-9
    assert results[0].filament_id == "GFG00"
    assert mqtt_client.state.extrusion_cali_status == "completed"


def test_parser_extrusion_cali_get_populates_history(mqtt_client):
    msg = {
        "print": {
            "command": "extrusion_cali_get",
            "nozzle_diameter": "0.4",
            "filaments": [
                {
                    "cali_idx": 0,
                    "name": "PLA — PA 0.04",
                    "filament_id": "GFG00",
                    "setting_id": "GFG00_60@BBL",
                    "nozzle_diameter": "0.4",
                    "nozzle_volume_type": "standard",
                    "extruder_id": 0,
                    "k_value": "0.040000",
                    "n_coef": "1.000000",
                }
            ],
        }
    }
    mqtt_client._on_message(None, None, _FakeMsg(msg))
    hist = mqtt_client.state.extrusion_cali_history
    assert len(hist) == 1
    assert hist[0].cali_idx == 0
    assert abs(hist[0].k_value - 0.04) < 1e-9


def test_parser_capability_flags_direct(mqtt_client):
    msg = {
        "print": {
            "command": "push_status",
            "support_auto_flow_calibration": True,
            "support_pa_calibration": True,
        }
    }
    mqtt_client._on_message(None, None, _FakeMsg(msg))
    assert mqtt_client.state.is_support_auto_flow_calibration is True
    assert mqtt_client.state.is_support_pa_calibration is True


def test_parser_capability_flags_via_func_bitfield(mqtt_client):
    """Legacy X1: capabilities advertised via func bitfield bits 15/16."""
    func = (1 << 15) | (1 << 16)  # both flow + PA
    msg = {"print": {"command": "push_status", "func": func}}
    mqtt_client._on_message(None, None, _FakeMsg(msg))
    assert mqtt_client.state.is_support_pa_calibration is True
    assert mqtt_client.state.is_support_auto_flow_calibration is True


def test_parser_mc_print_stage_idle_with_auto_cali_marks_completed(mqtt_client):
    """When session is running and printer reports stage IDLE on auto_cali_*
    gcode file, status flips to completed."""
    mqtt_client.state.extrusion_cali_status = "running"
    mqtt_client.state.gcode_file = "auto_cali_for_user_param.gcode"
    msg = {"print": {"command": "push_status", "mc_print_stage": 1, "gcode_file": "auto_cali_for_user_param.gcode"}}
    mqtt_client._on_message(None, None, _FakeMsg(msg))
    assert mqtt_client.state.extrusion_cali_status == "completed"
