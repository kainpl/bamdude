"""Every other printer reads as before (N01 stage A, spec r2 §3.2).

The hand-written formulas the helper replaced are kept here verbatim as the
reference: for the slots X1 / P1 / A1 / A2L / H2D / H2C / AMS-HT and the
external holders 254 / 255 actually report, each delegating function gives
what the old copy gave. The intentional changes are tested where they live:
the mounted hotend (``id < 0x10``), the AMS-HT runout correction and print
summary labels, the external spool's archive colour.
"""

import pytest

from backend.app.services import spool_assignment_notifications as notes, spool_single_slot, usage_tracker
from backend.app.services.bambu_mqtt import _fts_global_slot, decode_filam_bak_groups, unit_reported_absent
from backend.app.utils.tray_ids import NO_LAYOUT, decode_wire_slot, slot_key

# Realistic slots of the printers that exist: regular AMS 0-3, the A2L Lite (6), AMS-HT 128-135, externals.
SLOTS = [(a, t) for a in range(4) for t in range(4)] + [(6, t) for t in range(4)] + [(a, 0) for a in range(128, 136)]
EXTERNAL = [(255, 0), (255, 1)]
GLOBALS = [*range(16), *range(24, 28), *range(128, 136), 254, 255]


def _old_global_tray(ams_id, tray_id):  # spool_single_slot.global_tray, before
    if ams_id == 255:
        return 254 + tray_id
    if ams_id >= 128:
        return ams_id
    return ams_id * 4 + tray_id


def _old_slot_key(tray):  # usage_tracker._global_tray_key / print_usage_journal, before
    if tray >= 254:
        return (255, tray - 254)
    if tray >= 128:
        return (tray, 0)
    return (tray // 4, tray % 4)


def _old_decode(value):  # usage_tracker._decode_mqtt_mapping, one value, before
    if not isinstance(value, int) or value >= 65535:
        return -1
    ams_hw_id, slot = value >> 8, value & 0xFF
    if 0 <= ams_hw_id <= 3:
        return ams_hw_id * 4 + (slot & 0x03)
    if 128 <= ams_hw_id <= 135:
        return ams_hw_id
    if ams_hw_id in (254, 255):
        return 254 if slot != 255 else 255
    return -1


def _old_label(tray):  # spool_assignment_notifications._slot_label_from_global_tray, before
    if tray == 254:
        return "Ext-L"
    if tray == 255:
        return "Ext-R"
    if tray >= 128:
        return f"HT-{chr(65 + (tray - 128))}"
    if 24 <= tray <= 27:
        return f"Lite-{(tray % 4) + 1}"
    return f"{chr(65 + tray // 4)}{tray % 4 + 1}"


def _old_fts(value):  # bambu_mqtt._fts_global_slot, before
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return -1
    ams_id = (value >> 8) & 0xFF
    ams_id = 6 if ams_id == 16 else ams_id
    slot_id = value & 0xFF
    if 128 <= ams_id <= 143:
        return ams_id if slot_id == 0 else -1
    if 0 <= ams_id < 16 and 0 <= slot_id <= 3:
        return ams_id * 4 + slot_id
    return -1


@pytest.mark.parametrize("slot", SLOTS + EXTERNAL)
def test_stored_slot_to_global(slot):
    assert spool_single_slot.global_tray(*slot) == _old_global_tray(*slot)
    assert usage_tracker._global_tray_id(*slot) == _old_global_tray(*slot)
    assert notes._global_tray_from_assignment(*slot) == _old_global_tray(*slot)


@pytest.mark.parametrize("tray", GLOBALS)
def test_global_to_stored_slot(tray):
    assert slot_key(tray) == _old_slot_key(tray)
    assert usage_tracker._global_tray_key(tray) == _old_slot_key(tray)


WIRE = [(a << 8) | s for a in (0, 1, 2, 3) for s in (0, 1, 2, 3)] + [
    128 << 8,
    135 << 8,
    0xFE00,
    0xFF00,
    0xFEFF,
    0xFFFF,
    16 << 8,
    (6 << 8) | 1,
]


@pytest.mark.parametrize("value", WIRE)
def test_wire_decoding(value):
    new = decode_wire_slot(value)
    assert (-1 if new is None else new) == _old_decode(value)
    assert usage_tracker._decode_mqtt_mapping([value, 0]) in (None, [_old_decode(value), 0])


@pytest.mark.parametrize("value", [*WIRE, (5 << 8) | 2, (16 << 8) | 3, (140 << 8), (140 << 8) | 1, -1])
def test_fts_inlet_decoding(value):
    assert _fts_global_slot(value) == _old_fts(value)


@pytest.mark.parametrize("tray", GLOBALS)
def test_slot_labels(tray):
    assert notes._slot_label_from_global_tray(tray) == _old_label(tray)
    assert notes._slot_label_from_global_tray(tray, NO_LAYOUT) == _old_label(tray)


def test_backup_groups():
    def old(mask):
        members = [bit for bit in range(16) if mask & (1 << bit)]
        members.extend(128 + bit for bit in range(8) if mask & (1 << (16 + bit)))
        members.extend(24 + bit for bit in range(4) if mask & (1 << (24 + bit)))
        return members

    for mask in (0b1011, 1 << 17, (1 << 25) | 1, 0xFFFF_FFFF):
        assert decode_filam_bak_groups([mask]) == ([old(mask)] if old(mask) else [])


def test_unit_absence():
    # Units 0-3 are judged by their bit as before; AMS-HT / Lite bits are still not read.
    assert unit_reported_absent(1, [{"id": "0"}], "1") is True
    assert unit_reported_absent(0, [{"id": "1"}], "1") is False
    assert unit_reported_absent(128, [{"id": "0"}], "1") is False
    assert unit_reported_absent(6, [{"id": "0"}], "1") is False
    assert unit_reported_absent(2, [], "0") is True
