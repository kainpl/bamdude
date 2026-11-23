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
