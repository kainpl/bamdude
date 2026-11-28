"""H3C / P3C telemetry read through one numbering (N01 stage A).

``H3C_FRAME`` is trimmed from upstream Bambuddy's live H3C capture
(``test_h3c_ams3h.py``: firmware 01.01.03.00, AMS 3H 02.00.32.82, mid-print,
``tray_now`` "6", slot 7 empty, the hotend of dock 6 on the toolhead). Fields
the capture does not carry are SYNTHETIC and marked so: ``vir_slot``,
``device.holder`` and ``device.extruder.info[].temp``. Without ``vir_slot`` and
``holder`` the rack is told by the model's config (``default_nozzle_count``).

``P3C_FRAME`` is entirely synthetic, in the shape Bambu Studio v02.09.02.62
parses (AMS 3P unit 33, seven slots, inputs 249-255).
"""

import copy

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.printer_manager import ams_units_for, nozzle_rack_entries, printer_state_to_dict

_TRAY = {"state": 27, "tray_type": "PLA", "remain": -1, "tray_info_idx": "GFL99", "tag_uid": "0000000000000000"}

H3C_FRAME = {
    "command": "push_status",
    "ams": {
        "tray_now": "6",
        "tray_pre": "6",
        "tray_tar": "6",
        "tray_exist_bits": "17f0000000",
        "ams_exist_bits": "2000",
        "power_on_flag": False,
        "ams": [
            {
                "id": "32",
                "info": "240842026",
                "tray": [{**_TRAY, "id": str(k), "tray_color": "DE4343FF"} for k in range(7)]
                + [{"id": "7", "state": 160}, {**_TRAY, "id": "8", "tray_color": "61C680FF"}],
            }
        ],
    },
    "mapping": [8193, 8195, 8198, 8192, 8200, 8196, 8192, 8194, 8197],
    "device": {
        "extruder": {
            "state": 1,
            # ``temp`` is SYNTHETIC: 230 °C current, 240 °C target (bits 0-15 / 16-31).
            "info": [{"id": 0, "snow": 8198, "hnow": 0, "temp": (240 << 16) | 230, "info": 0b1010}],
        },
        "nozzle": {
            "info": [
                *[
                    {"id": 16 + addr, "addr_id": addr, "stat": 168, "type": "HS01", "diameter": 0.4}
                    for addr in range(6)
                ],
                {"id": 0, "addr_id": 6, "stat": 168, "type": "HS01", "diameter": 0.4},
                {"id": 23, "addr_id": 7, "stat": 168, "type": "HT01", "diameter": 0.4, "snow": 0xF800},
                {"id": 24, "addr_id": 8, "stat": 168, "type": "HS01", "diameter": 0.6},
            ]
        },
    },
    # SYNTHETIC: an H3C reports nine external inputs, 247-255 (Studio's 255 - addr).
    "vir_slot": [{"id": str(v), "tray_type": ""} for v in range(247, 256)],
    # ``nozzle_temper`` would report the hotend just put back in the rack (upstream f71e).
    "nozzle_temper": 35,
    "nozzle_target_temper": 0,
}

P3C_FRAME = {
    "command": "push_status",
    "ams": {
        "tray_now": "3",
        "tray_exist_bits": hex(sum(1 << (37 + k) for k in range(7)))[2:],
        "ams_exist_bits": "4000",
        "ams": [
            {
                "id": "33",
                "info": "7",
                "tray": [{**_TRAY, "id": str(k), "tray_color": "FFFFFFFF"} for k in range(7)],
                "zone_info": [
                    {"zone_id": 0, "zone_slots": [1, 3], "temp": 48, "humidity_raw": 18, "info": "20"},
                    {"zone_id": 1, "zone_slots": [0, 2, 4, 5, 6], "temp": 26, "humidity_raw": 41},
                ],
            }
        ],
    },
    "device": {
        "holder": {"info2": 0b101},  # rack type B (bits 1-4), calibrated
        "extruder": {"info": [{"id": 0, "snow": (33 << 8) | 3, "temp": 210}]},
    },
    "vir_slot": [{"id": str(v)} for v in range(249, 256)],
}


@pytest.fixture
def h3c():
    client = BambuMQTTClient(ip_address="192.0.2.1", serial_number="H3C0001", access_code="x", model="H3C")
    client._process_message({"print": copy.deepcopy(H3C_FRAME)})
    return client


@pytest.fixture
def p3c():
    client = BambuMQTTClient(ip_address="192.0.2.2", serial_number="P3C0001", access_code="x", model="P3C")
    client._process_message({"print": copy.deepcopy(P3C_FRAME)})
    return client


def test_h3c_is_one_toolhead_with_a_nine_slot_rack(h3c):
    layout = h3c.tray_layout()
    assert layout.direct_rack and layout.rack_size == 9
    assert h3c._is_dual_nozzle is False
    unit = ams_units_for(h3c.state, None, {})[0]
    assert (unit.ams_type, unit.slot_count, unit.is_rack_ams) == (6, 9, True)
    assert unit.is_ams_ht is False  # unit 32 is no longer read into the AMS-HT range


def test_h3c_active_tray_from_snow(h3c):
    assert h3c.state.tray_now == 34  # (32 << 8) | 6 -> 28 + 6


def test_h3c_bare_tray_now_without_snow_follows_the_rule(h3c):
    h3c._process_message({"print": {"ams": {"tray_now": "3"}}})
    assert h3c.state.tray_now == 31
    h3c._process_message({"print": {"ams": {"tray_now": "12"}}})  # beyond the rack: kept
    assert h3c.state.tray_now == 31
    h3c._process_message({"print": {"ams": {"tray_now": "254"}}})  # names no slot: kept
    assert h3c.state.tray_now == 31
    h3c._process_message({"print": {"ams": {"tray_now": "255"}}})
    assert h3c.state.tray_now == 255


def test_h3c_toolhead_temperature_ignores_nozzle_temper(h3c):
    temps = h3c.state.temperatures
    assert temps["nozzle"] == 230.0 and temps["nozzle_target"] == 240.0
    assert "nozzle_2" not in temps
    h3c._process_message({"print": {"nozzle_temper": 40, "nozzle_target_temper": 0}})
    assert h3c.state.temperatures["nozzle"] == 230.0


def test_h3c_empty_slot_from_its_bit(h3c):
    trays = {int(t["id"]): t for t in h3c.state.raw_data["ams"][0]["tray"]}
    assert trays[7]["exists"] is False and trays[8]["exists"] is True


def test_h3c_external_inputs_kept_by_id(h3c):
    h3c._process_message({"print": {"vir_slot": [{"id": "255", "tray_type": "PETG"}]}})
    ids = sorted(int(v["id"]) for v in h3c.state.raw_data["vt_tray"])
    assert ids == list(range(247, 256))  # 255 not folded onto 254, the other eight kept
    assert next(v for v in h3c.state.raw_data["vt_tray"] if v["id"] == "255")["tray_type"] == "PETG"


def test_h3c_rack_entries_carry_the_dock(h3c):
    entries = {e["addr_id"]: e for e in nozzle_rack_entries(h3c.state)}
    assert entries[6]["id"] == 0 and entries[6]["print_tool"] == 7  # on the toolhead
    assert entries[8]["nozzle_diameter"] == "0.6"


def test_h3c_status_says_direct_rack(h3c):
    status = printer_state_to_dict(h3c.state, printer_id=None, model="H3C")
    assert status["direct_rack"] is True and status["rack_size"] == 9
    assert status["tray_now"] == 34


def test_p3c_rack_from_holder_and_zones(p3c):
    layout = p3c.tray_layout()
    assert layout.direct_rack and layout.rack_size == 7
    assert p3c.state.tray_now == 40  # (33 << 8) | 3 -> 37 + 3
    unit = ams_units_for(p3c.state, None, {})[0]
    assert unit.ams_type == 7 and unit.slot_count == 7
    dry = [z for z in unit.drying_zones if z.can_dry]
    assert [z.slots for z in dry] == [[1, 3]] and dry[0].dry_status == 2
    # The unit reports no humidity of its own: the zone that does not dry gives it.
    assert unit.humidity == 41


def test_p3c_unit_leaves_on_its_own_exist_bit(p3c):
    p3c._process_message({"print": {"ams": {"ams": [], "ams_exist_bits": "4000"}}})
    assert [u["id"] for u in p3c.state.raw_data["ams"]] == ["33"]  # bit 14 still set: kept
    p3c._process_message({"print": {"ams": {"ams": [], "ams_exist_bits": "1"}}})
    assert p3c.state.raw_data["ams"] == []  # bit 14 clear: the AMS 3P is gone


def test_h3c_print_tray_changes_are_rack_slots_and_book_to_them(h3c):
    from backend.app.services.usage_tracker import _global_tray_key

    h3c._was_running = True
    h3c._completion_triggered = False
    h3c.state.last_loaded_tray = 34
    for snow, layer in ((8193, 12), (0xF800, 30)):  # slot 1, then PrintTool 8's external input
        h3c._process_message(
            {"print": {"layer_num": layer, "device": {"extruder": {"info": [{"id": 0, "snow": snow}]}}}}
        )
    assert [tray for tray, _ in h3c.state.tray_change_log] == [29, 248]
    layout = h3c.tray_layout()
    assert [_global_tray_key(tray, layout) for tray, _ in h3c.state.tray_change_log] == [(32, 1), (255, 3)]
