"""MQTT slot reports for upstream C17 and C14."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.printer_diagnostic import _external_storage_check


def _client(model):
    return BambuMQTTClient("127.0.0.1", "SYNTHETIC", "00000000", model=model)


def _status(client, **fields):
    client._process_message({"print": {"command": "push_status", **fields}})


@pytest.mark.parametrize("model", ["A1", "A1 Mini"])
def test_colour_delta_preserves_external_spool_and_status_confirms_assignment(model):
    client = _client(model)
    _status(client, vt_tray={"id": "254", "tray_type": "PLA", "tray_info_idx": "GFL99", "tray_color": "000000FF"})
    verified = Mock()
    client.on_assignment_verified = verified
    client.register_assignment_verification(255, 0, "GFA00", "FFFFFFFF", None)

    _status(client, vt_tray={"tray_color": "FFFFFFFF"})

    tray = client.state.raw_data["vt_tray"][0]
    assert (tray["id"], tray["tray_type"], tray["tray_info_idx"], tray["tray_color"]) == (
        "254",
        "PLA",
        "GFL99",
        "FFFFFFFF",
    )
    verified.assert_not_called()

    _status(client, vt_tray={"id": "254", "tray_type": "PLA", "tray_info_idx": "GFA00", "tray_color": "FFFFFFFF"})

    verified.assert_called_once_with(255, 0, True, {"tray_info_idx": "GFA00", "kprofile_applied": True})
    assert client._pending_assignments == {}


def test_external_readback_ignores_cached_id_and_command_echo():
    client = _client("A1")
    _status(client, vt_tray={"id": 254, "tray_type": "PLA", "tray_info_idx": "GFA00", "tray_color": "000000FF"})
    verified = Mock()
    client.on_assignment_verified = verified
    client.register_assignment_verification(255, 0, "GFA00", "FFFFFFFF", 7)

    client._process_message(
        {
            "print": {
                "command": "ams_filament_setting",
                "result": "success",
                "vt_tray": {"id": 254, "tray_info_idx": "GFA00", "cali_idx": 7},
            }
        }
    )
    _status(client, ams={"ams": [{"id": "0", "tray": [{"id": "0"}]}]})
    _status(client, vt_tray=[{"tray_color": "FFFFFFFF"}])
    verified.assert_not_called()

    _status(client, vt_tray=[{"tray_info_idx": "GFA00", "cali_idx": 7}])

    verified.assert_called_once_with(255, 0, True, {"tray_info_idx": "GFA00", "kprofile_applied": True})


def test_a_right_external_slot_report_cannot_confirm_the_left_slot():
    client = _client("H2D")
    left = {"id": 254, "tray_info_idx": "GFA00"}
    right = {"id": 255, "tray_info_idx": "GFA00"}
    _status(client, vir_slot=[left, right])
    verified = Mock()
    client.on_assignment_verified = verified
    for tray_id in (0, 1):
        client.register_assignment_verification(255, tray_id, "GFA00", "FFFFFFFF", None)

    _status(client, vt_tray=[{"tray_info_idx": "GFA00"}])
    verified.assert_not_called()

    _status(client, vir_slot=[right])
    verified.assert_called_once_with(255, 1, True, {"tray_info_idx": "GFA00", "kprofile_applied": True})
    assert (255, 0) in client._pending_assignments


def _mixed_status(client, lite_id):
    _status(
        client,
        ams={
            "ams": [
                {
                    "id": "0",
                    "info": "2003",
                    "tray": [
                        {"id": "0", "tray_type": "PETG", "tray_color": "FF0000FF", "tray_info_idx": "GFG99"},
                        {"id": "1", "tray_type": "PETG", "tray_color": "00FF00FF", "tray_info_idx": "GFG99"},
                    ],
                },
                {
                    "id": lite_id,
                    "info": "30001005",
                    "tray": [
                        {"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF", "tray_info_idx": "GFA00"},
                        {"id": "1", "tray_type": "PLA", "tray_color": "000000FF", "tray_info_idx": "GFA00"},
                    ],
                },
            ],
            "ams_exist_bits": "1001",
            "tray_exist_bits": "3000003",
            "tray_now": "255",
            "power_on_flag": True,
        },
    )


@pytest.mark.parametrize("lite_id", [16, "16"])
def test_a2l_mixed_units_preserve_other_slots_across_partial_frames(lite_id):
    client = _client("A2L")
    _mixed_status(client, lite_id)
    units = client.state.raw_data["ams"]
    assert [str(unit["id"]) for unit in units] == ["0", "6"]
    assert units[0]["tray"][0]["tray_type"] == "PETG"
    assert units[1]["tray"][0]["tray_type"] == "PLA"

    _status(client, ams={"ams": [{"id": 0, "tray": [{"id": 0, "tray_color": "112233FF"}]}]})
    _status(client, ams={"ams": [{"id": "6", "tray": [{"id": 0, "tray_color": "445566FF"}]}]})

    ams, lite = client.state.raw_data["ams"]
    assert (ams["id"], lite["id"]) == ("0", 6)
    assert [(t["tray_info_idx"], t["tray_color"]) for t in ams["tray"]] == [
        ("GFG99", "112233FF"),
        ("GFG99", "00FF00FF"),
    ]
    assert [(t["tray_info_idx"], t["tray_color"]) for t in lite["tray"]] == [
        ("GFA00", "445566FF"),
        ("GFA00", "000000FF"),
    ]


@pytest.mark.parametrize("model", ["A2L", "N9"])
def test_a2l_storage_diagnostic_follows_live_capability(model):
    state = SimpleNamespace(connected=True, store_to_sdcard=False)
    printer = SimpleNamespace(model=model)

    skip = _external_storage_check(state, printer)
    assert (skip.status, skip.params) == ("skip", {"reason": "unsupported_model"})

    state.print_option_support = {"save_remote_to_storage": True}
    assert _external_storage_check(state, printer).status == "fail"
