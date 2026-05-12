"""Tests compute_calibration_supports — per-model gating."""

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.printer_capabilities import compute_calibration_supports


def _state(*, pa: bool = False, flow: bool = False) -> PrinterState:
    s = PrinterState()
    s.is_support_pa_calibration = pa
    s.is_support_auto_flow_calibration = flow
    return s


def test_x1c_pa_auto_when_supported():
    caps = compute_calibration_supports(_state(pa=True, flow=True), "X1C", {})
    assert caps["pa_auto"] is True
    assert caps["flow_auto"] is True
    assert caps["pa_manual"] is True
    assert caps["flow_manual"] is True


def test_x1c_pa_auto_false_when_state_says_no():
    caps = compute_calibration_supports(_state(pa=False, flow=False), "X1C", {})
    assert caps["pa_auto"] is False
    assert caps["flow_auto"] is False


def test_p1s_no_auto_paths():
    # Even if state reports support, P1S doesn't have a lidar → auto blocked
    caps = compute_calibration_supports(_state(pa=True, flow=True), "P1S", {})
    assert caps["pa_auto"] is False
    assert caps["flow_auto"] is False
    assert caps["pa_manual"] is True


def test_a1_mini_manual_only():
    caps = compute_calibration_supports(_state(), "A1 Mini", {})
    assert caps["pa_auto"] is False
    assert caps["flow_auto"] is False
    assert caps["pa_manual"] is True
    assert caps["flow_manual"] is True


def test_h2d_dual_extruder():
    caps = compute_calibration_supports(_state(pa=True), "H2D", {})
    assert caps["dual_extruder"] is True
    assert len(caps["extruders"]) == 2
    assert caps["extruders"][0]["id"] == 0
    assert caps["extruders"][1]["id"] == 1


def test_x1c_single_extruder():
    caps = compute_calibration_supports(_state(), "X1C", {})
    assert caps["dual_extruder"] is False
    assert len(caps["extruders"]) == 1


def test_unknown_model_safe_defaults():
    caps = compute_calibration_supports(_state(pa=True), "UnknownModel", {})
    assert caps["pa_auto"] is False
    assert caps["pa_manual"] is True


def test_tower_modes_universal():
    caps = compute_calibration_supports(_state(), "P1S", {})
    assert caps["temp_tower"] is True
    assert caps["vol_speed_tower"] is True
    assert caps["vfa_tower"] is True
    assert caps["retraction_tower"] is True
