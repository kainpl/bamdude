"""Calibration mode metadata + math helpers + nozzle_id encoder.

Constants frozen from BS resources/calib/ — PA Line range, Flow Rate
9-block modifiers. Math helpers map UI input (best line index, best
block modifier) to K values / flow ratios. nozzle_id encoder mirrors
BS DeviceManager.cpp:338-350.
"""

from __future__ import annotations

from enum import Enum

# BS pa_line.3mf range: 0.0 → 0.1 step 0.002 = 50 lines (index 0..49)
PA_LINE_RANGE: tuple[float, float, float, int] = (0.0, 0.1, 0.002, 50)

# BS flowrate-test-pass1.3mf: 9 blocks
FLOW_RATE_COARSE_MODIFIERS: tuple[int, ...] = (-20, -15, -10, -5, 0, 5, 10, 15, 20)
# BS flowrate-test-pass2.3mf: 10 blocks, all DOWNWARD from the coarse pick.
# Verified against the shipped pass2.3mf object names (flowrate_m9..m1, _0)
# and BS's CalibrationWizardSavePage.cpp:1847-1851 (`for i in 0..9: -9 + i`).
FLOW_RATE_FINE_MODIFIERS: tuple[int, ...] = (-9, -8, -7, -6, -5, -4, -3, -2, -1, 0)


class CaliMode(str, Enum):
    PA_LINE = "pa_line"
    PA_PATTERN = "pa_pattern"
    PA_TOWER = "pa_tower"
    AUTO_PA_LINE = "auto_pa_line"
    FLOW_RATE = "flow_rate"
    TEMP_TOWER = "temp_tower"
    VOL_SPEED_TOWER = "vol_speed_tower"
    VFA_TOWER = "vfa_tower"
    RETRACTION_TOWER = "retraction_tower"


class CaliMethod(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"


class NozzleVolumeType(str, Enum):
    STANDARD = "standard"
    HIGH_FLOW = "high_flow"
    TPU_HIGH_FLOW = "tpu_high_flow"
    HYBRID = "hybrid"


# Maps for nozzle_id encoder
_VOL_TYPE_CHARS = {
    NozzleVolumeType.STANDARD: "S",
    NozzleVolumeType.HIGH_FLOW: "H",
    NozzleVolumeType.TPU_HIGH_FLOW: "U",
    NozzleVolumeType.HYBRID: "Y",
}

# Diameter to two-digit code (BS-format): 0.2→"00", 0.4→"20", 0.6→"40", 0.8→"60"
_DIAMETER_CODES = {
    0.2: "00",
    0.4: "20",
    0.6: "40",
    0.8: "60",
}


def generate_nozzle_id(vol_type: NozzleVolumeType, diameter: float) -> str:
    """Encode nozzle id per BS DeviceManager.cpp:338-350.

    Format: H + [S|H|U|Y] + diameter_code
    Examples: standard 0.4 → "HS20", high_flow 0.8 → "HH60".
    """
    code = _DIAMETER_CODES.get(round(diameter, 2))
    if code is None:
        raise ValueError(f"Unsupported nozzle diameter: {diameter}")
    return f"H{_VOL_TYPE_CHARS[vol_type]}{code}"


def compute_pa_k(line_index: int) -> float:
    """PA K = start + index * step. Index 0..49 for BS pa_line.3mf."""
    start, _end, step, count = PA_LINE_RANGE
    if line_index < 0 or line_index >= count:
        raise ValueError(f"line_index out of range: {line_index}")
    return start + line_index * step


def compute_flow_ratio_coarse(modifier_pct: int) -> float:
    """Flow ratio after coarse stage = 1.0 * (100 + mod) / 100."""
    if modifier_pct not in FLOW_RATE_COARSE_MODIFIERS:
        raise ValueError(f"Invalid coarse modifier: {modifier_pct}")
    return (100 + modifier_pct) / 100.0


def compute_flow_ratio_fine(coarse_ratio: float, modifier_pct: int) -> float:
    """Fine = coarse * (100 + mod) / 100."""
    if modifier_pct not in FLOW_RATE_FINE_MODIFIERS:
        raise ValueError(f"Invalid fine modifier: {modifier_pct}")
    return coarse_ratio * (100 + modifier_pct) / 100.0
