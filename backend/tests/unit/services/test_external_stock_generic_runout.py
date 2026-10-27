"""Generic P1S runout is external only with positive, same-client feed facts."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


def client():
    result = BambuMQTTClient(ip_address="127.0.0.1", serial_number="SYNTHETIC", access_code="test", model="P1S")
    result._was_running = True
    result._runout_seeded = True
    result.state.layer_num = 158
    result.state.total_layers = 640
    result.state.tray_now = 254
    result.state.hms_errors = [SimpleNamespace(full_code="03008004")]
    result.state.feed_telemetry.observe(
        {"print": {"ams": {"ams_exist_bits": "0", "ams": []}, "vt_tray": [{"id": 254, "tray_type": "PETG"}]}}, "P1S"
    )
    result.state.raw_data = {"ams": [], "vt_tray": [{"id": 254, "tray_type": "PETG"}]}
    result.on_usage_event = Mock()
    return result


def test_generic_runout_on_proven_single_external_feed_can_claim_stock():
    mqtt = client()
    mqtt._detect_runout_signals()
    mqtt.on_usage_event.assert_called_once_with("runout", "external", 254, 158)
    mqtt._detect_runout_signals()
    assert mqtt.on_usage_event.call_count == 1


@pytest.mark.parametrize(
    "uncertain",
    ["ams_unknown", "ams_present", "external_unknown", "two_holders", "dual_nozzle", "unloaded", "ams_feed", "jam"],
)
def test_generic_code_never_bypasses_ambiguous_feed_or_jam(uncertain):
    mqtt = client()
    telemetry = mqtt.state.feed_telemetry
    if uncertain == "ams_unknown":
        telemetry.ams_known = False
    elif uncertain == "ams_present":
        telemetry.ams_present = True
    elif uncertain == "external_unknown":
        telemetry.external_known = False
    elif uncertain == "two_holders":
        telemetry.external[255] = {"id": 255}
    elif uncertain == "dual_nozzle":
        mqtt._is_dual_nozzle = True
    elif uncertain == "unloaded":
        mqtt.state.tray_now = 255
    elif uncertain == "ams_feed":
        mqtt.state.tray_now = 0
    else:
        mqtt.state.hms_errors = [SimpleNamespace(full_code="12FF800000020001")]
    mqtt._detect_runout_signals()
    assert all(call.args[1] != "external" for call in mqtt.on_usage_event.call_args_list)
