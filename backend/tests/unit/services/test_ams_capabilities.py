"""Tests for compute_ams_supports — drives UI row visibility."""

from backend.app.services.ams_capabilities import compute_ams_supports
from backend.app.services.bambu_mqtt import PrinterState


def _s() -> PrinterState:
    return PrinterState()


def test_x1c_supports_all_four_basic_flags_but_not_ams_air_print():
    sup = compute_ams_supports(_s(), "X1C")
    assert sup["insertion_update"] is True
    assert sup["power_on_update"] is True
    assert sup["remain_capacity"] is True
    assert sup["auto_switch_filament"] is True
    # X1 has air-print in Print Options, NOT in AMS Settings dialog.
    assert sup["air_print_detect"] is False
    assert sup["firmware_switch"] is False
    assert sup["reorder"] is False


def test_a1_mini_no_rfid():
    sup = compute_ams_supports(_s(), "A1 Mini")
    assert sup["insertion_update"] is False
    assert sup["power_on_update"] is False
    assert sup["remain_capacity"] is False
    # Air-print lives in AMS settings on A1 series.
    assert sup["air_print_detect"] is True


def test_a1_full_has_firmware_switch_and_air_print():
    sup = compute_ams_supports(_s(), "A1")
    assert sup["insertion_update"] is True
    assert sup["firmware_switch"] is True
    assert sup["air_print_detect"] is True
    assert sup["reorder"] is False


def test_h2d_supports_reorder():
    sup = compute_ams_supports(_s(), "H2D")
    assert sup["reorder"] is True
    assert sup["firmware_switch"] is False
    assert sup["air_print_detect"] is False


def test_unknown_model_safe_defaults_all_false():
    sup = compute_ams_supports(_s(), "DefinitelyNotABambu")
    for key in (
        "insertion_update",
        "power_on_update",
        "remain_capacity",
        "auto_switch_filament",
        "air_print_detect",
        "firmware_switch",
        "reorder",
    ):
        assert sup[key] is False


def test_empty_model_safe_defaults():
    sup = compute_ams_supports(_s(), None)
    assert sup["insertion_update"] is False
    sup2 = compute_ams_supports(_s(), "")
    assert sup2["insertion_update"] is False


def test_p1s_full_ams():
    sup = compute_ams_supports(_s(), "P1S")
    assert sup["insertion_update"] is True
    assert sup["auto_switch_filament"] is True
    assert sup["air_print_detect"] is False  # not A1 family


def test_all_keys_always_present():
    sup = compute_ams_supports(_s(), "X1C")
    for key in (
        "insertion_update",
        "power_on_update",
        "remain_capacity",
        "auto_switch_filament",
        "air_print_detect",
        "firmware_switch",
        "reorder",
    ):
        assert key in sup
