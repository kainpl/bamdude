"""``utils/tray_ids`` — one numbering for every printer (N01 stage A).

The shared table ``frontend/src/__tests__/fixtures/trayIdCases.json`` is read
by this test and by ``frontend/src/__tests__/utils/trayIds.test.ts``: the two
twins answer it the same. The rest are the backend-only parts (bit masks, the
wire form, the holder, the sticky type).
"""

import json
from pathlib import Path

import pytest

from backend.app.utils.tray_ids import (
    NO_LAYOUT,
    AmsType,
    TrayLayout,
    ams_exist_bit,
    ams_type_from_info,
    decode_wire_slot,
    direct_rack_type_from_holder,
    external_label,
    external_print_tool,
    external_slot_index,
    external_tray_id,
    fallback_ams_type,
    filam_bak_members,
    global_tray_id,
    model_is_direct_rack,
    stamp_ams_type,
    tray_flag_bit,
    unit_name,
)

CASES = json.loads(
    (Path(__file__).resolve().parents[3] / "frontend/src/__tests__/fixtures/trayIdCases.json").read_text(
        encoding="utf-8"
    )
)


def _layout(name: str) -> TrayLayout:
    spec = CASES["layouts"][name]
    return TrayLayout(
        units=tuple((uid, AmsType(kind)) for uid, kind in spec["units"]),
        direct_rack=spec["direct_rack"],
        rack_size=spec["rack_size"],
    )


@pytest.mark.parametrize("case", CASES["global"], ids=lambda c: f"{c['ams']}-{c['slot']}-{c['type']}")
def test_global_tray_id(case):
    assert global_tray_id(case["ams"], case["slot"], case["type"]) == case["global"]


@pytest.mark.parametrize("case", CASES["external"], ids=lambda c: str(c["id"]))
def test_external_inputs(case):
    assert external_slot_index(case["id"]) == case["index"]
    assert external_tray_id(case["index"]) == case["id"]
    assert external_print_tool(case["id"]) == case["print_tool"]
    assert external_label(case["id"]) == case["label"]


@pytest.mark.parametrize("case", CASES["slot_key"], ids=lambda c: f"{c['layout']}-{c['global']}")
def test_slot_key(case):
    assert _layout(case["layout"]).slot_key(case["global"]) == tuple(case["key"])


@pytest.mark.parametrize("case", CASES["accepts"], ids=lambda c: f"{c['layout']}-{c['ams']}-{c['slot']}")
def test_accepts_slot(case):
    assert _layout(case["layout"]).accepts_slot(case["ams"], case["slot"]) is case["ok"]


@pytest.mark.parametrize("case", CASES["unit_name"], ids=lambda c: str(c["ams"]))
def test_unit_name(case):
    assert unit_name(case["ams"], case["type"]) == case["name"]


def test_type_from_info_bits_0_to_3():
    # The H3C's own AMS 3H info (upstream capture): 0x240842026 -> 6.
    assert ams_type_from_info("240842026") == AmsType.ZMULTI_A
    assert ams_type_from_info("10001003") == AmsType.N3F
    assert ams_type_from_info("0") is None  # EXT_SPOOL / never typed: the id decides
    assert ams_type_from_info("zz") is None


def test_fallback_type_by_id():
    assert fallback_ams_type(6) == AmsType.AMS_LITE_MIXED
    assert fallback_ams_type(32) == AmsType.ZMULTI_A
    assert fallback_ams_type(33) == AmsType.ZMULTI_B
    assert fallback_ams_type(130) == AmsType.N3S
    assert fallback_ams_type(2) == AmsType.AMS


def test_type_is_sticky_across_a_delta_without_info():
    unit = {"id": "32", "info": "240842026"}
    stamp_ams_type(unit)
    assert unit["ams_type"] == AmsType.ZMULTI_A
    merged = {**unit, **{"id": "32", "tray": []}}  # a delta: no info
    stamp_ams_type(merged)
    assert merged["ams_type"] == AmsType.ZMULTI_A


def test_flag_and_exist_bits():
    assert tray_flag_bit(0, 2) == 2
    assert tray_flag_bit(6, 1) == 25  # A2L Lite at its global id
    assert tray_flag_bit(129, 0) == 17  # AMS-HT 16 + (id - 128)
    assert tray_flag_bit(32, 7) == 35  # AMS 3H at 28 + k (the capture's empty slot 7)
    assert tray_flag_bit(33, 6) == 43
    assert ams_exist_bit(1) == 1
    assert ams_exist_bit(130) == 6
    assert ams_exist_bit(6) == 12
    assert ams_exist_bit(32) == 13
    assert ams_exist_bit(33) == 14


def test_filam_bak_counts_ams3h_bits_only_with_an_ams3h():
    mask = (1 << 28) | (1 << 30) | (1 << 2)
    assert filam_bak_members(mask) == [2]
    assert filam_bak_members(mask, {32: AmsType.ZMULTI_A}) == [2, 28, 30]
    # A ZMULTI_B group is not shown (Studio reads its bits as 28 + slot too, naming no tray).
    assert filam_bak_members(mask, {33: AmsType.ZMULTI_B}) == [2]


def test_decode_wire_slot_matches_the_old_readers_and_reads_the_rack():
    assert decode_wire_slot((1 << 8) | 2) == 6
    assert decode_wire_slot((0 << 8) | 6) == 2  # slot bits & 3, as every reader took it
    assert decode_wire_slot(130 << 8) == 130
    assert decode_wire_slot(0xFE00) == 254
    assert decode_wire_slot(0xFF00) == 254  # off a direct rack 255/0 is the one external
    assert decode_wire_slot(0xFFFF) is None
    assert decode_wire_slot((16 << 8) | 1) is None  # the A2L's physical unit: unmapped, as before
    h3c = TrayLayout(units=((32, AmsType.ZMULTI_A),), direct_rack=True, rack_size=9)
    assert h3c.decode_wire_slot(8198) == 34  # (32 << 8) | 6 — the capture's snow
    assert h3c.decode_wire_slot(0xF800) == 248  # an external input keeps its own id
    assert h3c.decode_wire_slot(0xFF00) == 255
    assert h3c.decode_wire_slot((32 << 8) | 9) is None  # beyond the rack


def test_holder_rack_type():
    assert direct_rack_type_from_holder({"info2": 0b10}) == 1  # bits 1-4
    assert direct_rack_type_from_holder({"info2": 0b101}) == 2
    assert direct_rack_type_from_holder({"info": 3}) == 0  # the H2C: RackType_AB
    assert direct_rack_type_from_holder({"stat": 1}) is None


def test_layout_build_from_holder_model_and_inputs():
    nine = [{"id": str(v)} for v in range(247, 256)]
    assert TrayLayout.build([{"id": "32"}], nine, model="H3C").rack_size == 9
    # No holder (the H3C's own frames): the config's default_nozzle_count > 2 says direct rack.
    layout = TrayLayout.build([{"id": "33"}], [], model="P3C")
    assert layout.direct_rack and layout.rack_size == 7
    assert not TrayLayout.build([{"id": "0"}], [], model="H2C").direct_rack
    # A holder that says RackType_AB wins over a model config.
    assert not TrayLayout.build([], [], rack_type=0, model="H3C").direct_rack
    assert model_is_direct_rack("N10") and model_is_direct_rack("N11") and not model_is_direct_rack("X1C")


def test_raw_slot_rule():
    h3c = TrayLayout(units=((32, AmsType.ZMULTI_A),), direct_rack=True, rack_size=9)
    assert h3c.raw_slot_to_global(6) == 34
    assert h3c.raw_slot_to_global(9) is None
    assert h3c.raw_slot_to_global(254) is None
    p3c = TrayLayout(units=((33, AmsType.ZMULTI_B),), direct_rack=True, rack_size=7)
    assert p3c.raw_slot_to_global(6) == 43
    assert p3c.raw_slot_to_global(7) is None
    assert NO_LAYOUT.raw_slot_to_global(3) is None


def test_journal_trays():
    h3c = TrayLayout(units=((32, AmsType.ZMULTI_A),), direct_rack=True, rack_size=9)
    assert h3c.is_journal_tray(34) and h3c.is_journal_tray(247)
    assert not h3c.is_journal_tray(255)  # always out: also "nothing loaded"
    assert NO_LAYOUT.is_journal_tray(254) and not NO_LAYOUT.is_journal_tray(30)
    p3c = TrayLayout(units=((33, AmsType.ZMULTI_B),), direct_rack=True, rack_size=7)
    assert not p3c.is_journal_tray(248)  # PrintTool 8 of a seven-tool rack
