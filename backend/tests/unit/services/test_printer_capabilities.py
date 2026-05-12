"""Tests for compute_printer_supports — Print Options + Parts row visibility."""

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.printer_capabilities import compute_printer_supports


def _supports(model: str | None):
    return compute_printer_supports(PrinterState(), model, module_vers={})


def test_x1c_supports_ai_monitoring_and_door():
    s = _supports("X1C")
    assert s["spaghetti_detector"] is True
    assert s["nozzleclumping_detector"] is True
    assert s["airprinting_detector"] is True
    assert s["first_layer_inspector"] is True
    assert s["filament_tangle"] is True
    assert s["nozzle_blob"] is True
    assert s["open_door_check"] is True
    assert s["auto_recovery"] is True
    assert s["sound"] is True
    assert s["parts_dual"] is False
    assert s["parts_editable"] is False


def test_a1_mini_no_ai_no_blob():
    s = _supports("A1 Mini")
    assert s["spaghetti_detector"] is False
    assert s["nozzle_blob"] is False
    assert s["auto_recovery"] is True
    assert s["sound"] is True


def test_h2d_dual_parts():
    s = _supports("H2D")
    assert s["parts_dual"] is True
    assert s["spaghetti_detector"] is True


def test_h2d_pro_purify_air():
    s = _supports("H2D Pro")
    assert s["purify_air"] is True


def test_p1s_no_ai_no_door():
    s = _supports("P1S")
    assert s["spaghetti_detector"] is False
    assert s["open_door_check"] is False
    assert s["auto_recovery"] is True


def test_unknown_model_safe_defaults():
    s = _supports("DefinitelyNotABambu")
    assert s["auto_recovery"] is True
    assert s["sound"] is True
    assert s["spaghetti_detector"] is False
    assert s["parts_dual"] is False


def test_empty_model_safe_defaults():
    assert _supports(None)["auto_recovery"] is True
    assert _supports("")["spaghetti_detector"] is False


def test_all_supports_keys_present():
    s = _supports("X1C")
    for key in (
        "spaghetti_detector",
        "pileup_detector",
        "nozzleclumping_detector",
        "airprinting_detector",
        "first_layer_inspector",
        "ai_monitoring",
        "filament_tangle",
        "nozzle_blob",
        "fod_check",
        "displacement_detection",
        "open_door_check",
        "purify_air",
        "auto_recovery",
        "sound",
        "save_remote_to_storage",
        "snapshot",
        "plate_type",
        "plate_align",
        "parts_editable",
        "parts_dual",
    ):
        assert key in s, f"missing key: {key}"
