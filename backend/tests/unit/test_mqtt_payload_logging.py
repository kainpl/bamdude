"""The raw debug log of an inbound printer report keeps bounded diagnostic fields."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


@pytest.mark.parametrize(
    ("command", "fields", "expected"),
    [
        (
            "ams_change_filament",
            {
                "target": 255,
                "slot_id": 255,
                "ams_id": 0,
                "extruder_id": "1",
                "curr_temp": 215,
                "tar_temp": "220",
                "result": "success",
                "err_code": 0,
            },
            {
                "target": 255,
                "slot_id": 255,
                "ams_id": 0,
                "extruder_id": "1",
                "curr_temp": 215,
                "tar_temp": "220",
                "result": "success",
                "err_code": 0,
                "sequence_id": "0",
            },
        ),
        ("ams_control", {"param": "resume"}, {"param": "resume", "sequence_id": "0"}),
        ("ams_control", {"param": "OPAQUE_PARAM_CANARY"}, {"sequence_id": "0"}),
        ("ams_change_filament", {"target": True, "slot_id": ["OPAQUE_SLOT_CANARY"]}, {"sequence_id": "0"}),
        (
            "ams_filament_setting",
            {"ams_id": 1, "tray_id": "2", "result": "success"},
            {"ams_id": 1, "tray_id": "2", "result": "success", "sequence_id": "0"},
        ),
        ("some_new_command", {"target": 255, "param": "OPAQUE_PARAM_CANARY"}, {"sequence_id": "0"}),
        ("push_status", {"target": 255}, None),
        ("project_file", {"result": "fail"}, None),
        ("project_file", {"result": "success", "use_ams": True}, None),
    ],
)
def test_report_debug_logs_only_selected_diagnostic_fields(caplog, command, fields, expected):
    client = BambuMQTTClient(ip_address="192.0.2.11", serial_number="TEST123", access_code="53296742")
    report = {
        "print": {
            "command": command,
            "sequence_id": "0",
            "url": "https://example.invalid/model.gcode?grant=OPAQUE_URL_CANARY",
            "access_code": "OPAQUE_ACCESS_CANARY",
            "metadata": {"reference": "OPAQUE_NESTED_CANARY"},
            **fields,
        }
    }
    msg = SimpleNamespace(topic=client.topic_subscribe, payload=json.dumps(report).encode())
    with caplog.at_level(logging.DEBUG, logger="backend.app.services.bambu_mqtt"):
        client._on_message(None, None, msg)

    messages = [record.getMessage() for record in caplog.records if record.name.endswith(".bambu_mqtt")]
    assert any(f"Received command response: {command}" in line for line in messages)
    assert all("OPAQUE_" not in line and "https://" not in line for line in messages)
    summaries = [json.loads(line.split("  fields: ", 1)[1]) for line in messages if "  fields: " in line]
    assert summaries == ([] if expected is None else [expected])
    if command == "project_file" and fields["result"] == "success":
        assert any("source=report flags={'use_ams': True}" in line for line in messages)
        assert client.state.current_project_url == report["print"]["url"]


@pytest.mark.parametrize(
    ("command", "label"),
    [
        ("ams_filament_setting", "refused"),
        ("ams_filament_drying", "response"),
        ("ams_get_rfid", "response"),
        ("extrusion_cali_set", "response"),
        ("extrusion_cali_del", "response"),
    ],
)
def test_verdict_logs_keep_codes_and_leave_full_reply_for_waiters(caplog, command, label):
    client = BambuMQTTClient(ip_address="192.0.2.11", serial_number="TEST123", access_code="53296742")
    client._pending_cali_acks["1"] = None
    client._pending_rfid_acks["1"] = None
    detail = "OPAQUE_PRINTER_TEXT"
    report = {
        "command": command,
        "sequence_id": "1",
        "result": "fail",
        "err_code": 451,
        "ams_id": 0,
        "reason": detail,
        "metadata": {"reference": detail},
    }
    msg = SimpleNamespace(topic=client.topic_subscribe, payload=json.dumps({"print": report}).encode())
    with caplog.at_level(logging.DEBUG, logger="backend.app.services.bambu_mqtt"):
        client._on_message(None, None, msg)

    info = next(r.getMessage() for r in caplog.records if f"{command} {label}:" in r.getMessage())
    assert "err_code=451" in info and "result=fail" in info and "sequence_id=1" in info
    assert detail not in caplog.text
    if command in ("extrusion_cali_set", "extrusion_cali_del"):
        assert client._pending_cali_acks["1"] == report
    if command == "ams_get_rfid":
        assert client._pending_rfid_acks["1"] == report


def test_developer_mode_probe_does_not_log_unknown_verdict_details(caplog):
    client = BambuMQTTClient(ip_address="192.0.2.11", serial_number="TEST123", access_code="53296742")
    client._dev_mode_probe_seq = "1"
    report = {"command": "ams_filament_setting", "sequence_id": "1", "result": "fail", "reason": "OPAQUE_PROBE"}
    msg = SimpleNamespace(topic=client.topic_subscribe, payload=json.dumps({"print": report}).encode())
    with caplog.at_level(logging.DEBUG, logger="backend.app.services.bambu_mqtt"):
        client._on_message(None, None, msg)

    assert "Developer mode probe: UNKNOWN" in caplog.text
    assert "OPAQUE_PROBE" not in caplog.text
    assert client._dev_mode_probe_seq is None


@pytest.mark.parametrize("source", ["request", "report"])
@pytest.mark.parametrize(("path", "plate"), [("Metadata/plate_2.gcode", "plate=2"), ("private.gcode", None)])
def test_project_file_logs_only_plate_number_and_typed_mapping(caplog, source, path, plate):
    client = BambuMQTTClient(ip_address="192.0.2.11", serial_number="TEST123", access_code="53296742")
    mapping = [1, -1, "OPAQUE_MAPPING", {"token": "OPAQUE_NESTED_MAPPING"}, True, 53296742, 255]
    report = {"command": "project_file", "result": "success", "param": path, "ams_mapping": mapping, "use_ams": True}
    topic = client.topic_publish if source == "request" else client.topic_subscribe
    msg = SimpleNamespace(topic=topic, payload=json.dumps({"print": report}).encode())
    with caplog.at_level(logging.DEBUG, logger="backend.app.services.bambu_mqtt"):
        client._on_message(None, None, msg)

    assert client._captured_print_param == path
    assert client._captured_ams_mapping == mapping
    assert path not in caplog.text
    assert "OPAQUE_" not in caplog.text
    assert "53296742" not in caplog.text
    assert "[1, -1, None, None, None, None, 255]" in caplog.text
    assert f"source={source} flags=" in caplog.text
    assert ("Captured plate from print command" in caplog.text) is (plate is not None)
    if plate:
        assert plate in caplog.text
