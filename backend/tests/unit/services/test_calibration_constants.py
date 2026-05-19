"""Tests calibration mode metadata + nozzle_id encoder."""

from backend.app.services.calibration_constants import (
    FLOW_RATE_COARSE_MODIFIERS,
    FLOW_RATE_FINE_MODIFIERS,
    PA_LINE_RANGE,
    CaliMode,
    NozzleVolumeType,
    compute_flow_ratio_coarse,
    compute_flow_ratio_fine,
    compute_pa_k,
    generate_nozzle_id,
)


def test_pa_line_range_constants():
    start, end, step, count = PA_LINE_RANGE
    assert start == 0.0
    assert end == 0.1
    assert abs(step - 0.002) < 1e-9
    assert count == 50


def test_flow_rate_modifiers():
    assert FLOW_RATE_COARSE_MODIFIERS == (-20, -15, -10, -5, 0, 5, 10, 15, 20)
    # BS pass2.3mf: 10 blocks downward from the coarse pick — see
    # flowrate_m9..m1, flowrate_0 (BS CalibrationWizardSavePage.cpp:1847).
    assert FLOW_RATE_FINE_MODIFIERS == (-9, -8, -7, -6, -5, -4, -3, -2, -1, 0)


def test_nozzle_id_standard_0_4():
    assert generate_nozzle_id(NozzleVolumeType.STANDARD, 0.4) == "HS20"


def test_nozzle_id_high_flow_0_4():
    assert generate_nozzle_id(NozzleVolumeType.HIGH_FLOW, 0.4) == "HH20"


def test_nozzle_id_tpu_high_flow_0_2():
    assert generate_nozzle_id(NozzleVolumeType.TPU_HIGH_FLOW, 0.2) == "HU00"


def test_nozzle_id_hybrid_0_8():
    assert generate_nozzle_id(NozzleVolumeType.HYBRID, 0.8) == "HY60"


def test_compute_pa_k():
    assert abs(compute_pa_k(0) - 0.0) < 1e-9
    assert abs(compute_pa_k(24) - 0.048) < 1e-9
    assert abs(compute_pa_k(49) - 0.098) < 1e-9


def test_compute_flow_ratio_coarse():
    assert abs(compute_flow_ratio_coarse(0) - 1.0) < 1e-9
    assert abs(compute_flow_ratio_coarse(10) - 1.10) < 1e-9
    assert abs(compute_flow_ratio_coarse(-15) - 0.85) < 1e-9


def test_compute_flow_ratio_fine():
    # Modifier ∈ FLOW_RATE_FINE_MODIFIERS = (-9..0); fine = coarse·(100+m)/100.
    assert abs(compute_flow_ratio_fine(1.0, 0) - 1.0) < 1e-9
    assert abs(compute_flow_ratio_fine(1.05, -5) - 0.9975) < 1e-9
    assert abs(compute_flow_ratio_fine(1.2, -9) - 1.092) < 1e-9


def test_cali_mode_enum():
    assert CaliMode.PA_LINE.value == "pa_line"
    assert CaliMode.FLOW_RATE.value == "flow_rate"
    assert CaliMode.AUTO_PA_LINE.value == "auto_pa_line"
