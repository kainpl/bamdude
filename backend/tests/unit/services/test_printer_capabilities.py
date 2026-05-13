"""Tests for compute_printer_supports — Print Options + Parts row visibility."""

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.printer_capabilities import compute_calibration_supports, compute_printer_supports


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


# ---------- compute_calibration_supports ----------


def test_cali_supports_sidecar_offline_gates_tower_modes():
    """Without a connected slicer sidecar, STL/STEP-geometry modes are off."""
    s = compute_calibration_supports(PrinterState(), "X1C", module_vers={}, slicer_sidecar_available=False)
    # Pre-sliced 3MFs always on
    assert s["pa_manual"] is True
    assert s["flow_manual"] is True
    # STL/STEP-geometry gated
    assert s["temp_tower"] is False
    assert s["vol_speed_tower"] is False
    assert s["vfa_tower"] is False
    assert s["retraction_tower"] is False
    assert s["slicer_sidecar_available"] is False


def test_cali_supports_sidecar_online_enables_tower_modes():
    s = compute_calibration_supports(PrinterState(), "X1C", module_vers={}, slicer_sidecar_available=True)
    assert s["temp_tower"] is True
    assert s["vol_speed_tower"] is True
    assert s["vfa_tower"] is True
    assert s["retraction_tower"] is True
    assert s["slicer_sidecar_available"] is True


def test_cali_supports_pa_auto_requires_lidar():
    """pa_auto / flow_auto gate on lidar model + push flag, independent of sidecar."""
    # X1C has lidar — pa_auto + flow_auto unlocked when state reports support
    state = PrinterState()
    state.is_support_pa_calibration = True
    state.is_support_auto_flow_calibration = True
    s = compute_calibration_supports(state, "X1C", module_vers={}, slicer_sidecar_available=False)
    assert s["pa_auto"] is True
    assert s["flow_auto"] is True

    # P1S has no lidar — pa_auto off regardless of state flags
    s = compute_calibration_supports(state, "P1S", module_vers={}, slicer_sidecar_available=True)
    assert s["pa_auto"] is False
    assert s["flow_auto"] is False
