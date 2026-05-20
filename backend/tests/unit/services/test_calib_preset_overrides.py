"""Unit tests for per-mode preset overrides applied before sidecar slice.

Pins the format / key shapes so future preset format changes don't
silently regress the patch.
"""

from __future__ import annotations

import json

from backend.app.services.calib_preset_overrides import (
    apply_pa_line_filament_overrides,
    apply_pa_line_printer_overrides,
    apply_pa_line_process_overrides,
    apply_pa_pattern_filament_overrides,
    apply_pa_pattern_printer_overrides,
    apply_pa_pattern_process_overrides,
    apply_pa_tower_filament_overrides,
    apply_pa_tower_process_overrides,
    apply_retraction_printer_overrides,
    apply_retraction_process_overrides,
    apply_temp_filament_overrides,
    apply_temp_printer_overrides,
    apply_temp_process_overrides,
)


def test_pa_pattern_process_overrides_pin_core_pa_pattern_hardcodes():
    """Operator's preset may carry wall_loops=4 (BS default), but PA
    Pattern wizard always forces 3. Likewise initial_layer_speed → 30,
    brim_type → no_brim, print_sequence → by layer."""
    preset = json.dumps({"wall_loops": "4", "initial_layer_speed": ["50"], "brim_type": "outer_only"})
    out = json.loads(apply_pa_pattern_process_overrides(preset, nozzle_diameter=0.4))
    assert out["wall_loops"] == "3"
    assert out["skirt_loops"] == "0"
    assert out["brim_type"] == "no_brim"
    assert out["enable_wrapping_detection"] == "0"
    assert out["print_sequence"] == "by layer"
    assert out["initial_layer_speed"] == ["30"]


def test_pa_pattern_process_overrides_compute_line_widths_from_nozzle():
    out = json.loads(apply_pa_pattern_process_overrides("{}", nozzle_diameter=0.4))
    assert out["line_width"] == "0.4500"
    assert out["initial_layer_line_width"] == "0.5600"

    out6 = json.loads(apply_pa_pattern_process_overrides("{}", nozzle_diameter=0.6))
    assert out6["line_width"] == "0.6750"
    assert out6["initial_layer_line_width"] == "0.8400"


def test_pa_pattern_process_overrides_pin_layer_heights():
    """SuggestedConfigCalibPAPattern.float_pairs hard-codes the layer
    heights — needed because the Python pattern generator anchors its
    pre-baked print_z entries to BS-default (0.25 first, 0.2 rest).
    Patch the preset so slicer's layer planner uses the same Zs."""
    out = json.loads(apply_pa_pattern_process_overrides("{}", nozzle_diameter=0.4))
    assert out["initial_layer_print_height"] == "0.25"
    assert out["layer_height"] == "0.2"


def test_pa_pattern_filament_overrides_disable_retract_wipe_on_layer_change():
    preset = json.dumps({"filament_retract_when_changing_layer": ["1"], "filament_wipe": ["1"]})
    out = json.loads(apply_pa_pattern_filament_overrides(preset))
    assert out["filament_retract_when_changing_layer"] == ["0"]
    assert out["filament_wipe"] == ["0"]


def test_pa_pattern_printer_overrides_disable_wipe_retract_resonance():
    preset = json.dumps({"wipe": ["1"], "retract_when_changing_layer": ["1"], "resonance_avoidance": "1"})
    out = json.loads(apply_pa_pattern_printer_overrides(preset))
    assert out["wipe"] == ["0"]
    assert out["retract_when_changing_layer"] == ["0"]
    assert out["resonance_avoidance"] == "0"


def test_pa_tower_filament_overrides_pin_slow_down_layer_time():
    preset = json.dumps({"slow_down_layer_time": ["5"]})
    out = json.loads(apply_pa_tower_filament_overrides(preset))
    assert out["slow_down_layer_time"] == ["1"]


def test_pa_tower_process_overrides_disable_wrapping_detection():
    preset = json.dumps({"enable_wrapping_detection": "1"})
    out = json.loads(apply_pa_tower_process_overrides(preset))
    assert out["enable_wrapping_detection"] == "0"


def test_temp_process_overrides_force_disable_support():
    """Temp Tower must force supports off — a sidecar CLI slice has no GUI
    cascade, so an operator process preset with supports enabled would
    otherwise wrap the self-supporting tower in support material."""
    preset = json.dumps({"enable_support": "1", "enable_wrapping_detection": "1", "wall_loops": "4"})
    out = json.loads(apply_temp_process_overrides(preset))
    assert out["enable_support"] == "0"
    assert out["enable_wrapping_detection"] == "0"
    # Unrelated keys (the operator's process preset) are left intact.
    assert out["wall_loops"] == "4"


def test_temp_filament_overrides_pin_start_temp():
    """Both nozzle-temperature keys are pinned to the sweep start (hot end);
    the descending per-layer ramp is inserted post-slice, not here."""
    preset = json.dumps({"nozzle_temperature": ["220"], "nozzle_temperature_initial_layer": ["220"]})
    out = json.loads(apply_temp_filament_overrides(preset, start_temp=250))
    assert out["nozzle_temperature"] == ["250"]
    assert out["nozzle_temperature_initial_layer"] == ["250"]


def test_temp_printer_overrides_disable_resonance_avoidance():
    preset = json.dumps({"resonance_avoidance": "1"})
    out = json.loads(apply_temp_printer_overrides(preset))
    assert out["resonance_avoidance"] == "0"


def test_retraction_process_overrides_disable_wrapping_and_support():
    preset = json.dumps({"enable_wrapping_detection": "1", "enable_support": "1"})
    out = json.loads(apply_retraction_process_overrides(preset))
    assert out["enable_wrapping_detection"] == "0"
    assert out["enable_support"] == "0"


def test_retraction_process_overrides_pin_layer_heights():
    """layer_height / initial_layer_print_height are process-level keys —
    pinned to 0.2 mm because the ramp bands by 1 mm of print Z. They must
    NOT be carried as per-object metadata (StaticPrintConfigs exit -100)."""
    preset = json.dumps({"layer_height": "0.28", "initial_layer_print_height": "0.32"})
    out = json.loads(apply_retraction_process_overrides(preset))
    assert out["layer_height"] == "0.2"
    assert out["initial_layer_print_height"] == "0.2"


def test_retraction_printer_overrides_force_slicer_retraction():
    """use_firmware_retraction must be off so retraction is slicer-side
    G1 E moves the patcher can rewrite, not firmware G10/G11."""
    preset = json.dumps({"use_firmware_retraction": "1", "resonance_avoidance": "1"})
    out = json.loads(apply_retraction_printer_overrides(preset))
    assert out["use_firmware_retraction"] == "0"
    assert out["resonance_avoidance"] == "0"


def test_retraction_printer_overrides_bump_low_max_layer_height():
    """max_layer_height entries below the 0.2 mm calibration layer height
    are bumped up so the slicer accepts the forced layer height."""
    preset = json.dumps({"max_layer_height": ["0.12", "0.25"]})
    out = json.loads(apply_retraction_printer_overrides(preset))
    assert out["max_layer_height"] == ["0.2", "0.25"]


def test_overrides_passthrough_invalid_json():
    """If a preset doesn't parse as JSON (e.g. wrapped stub), the
    override is a no-op — pass-through so the sidecar gets to decide."""
    junk = "not really json"
    assert apply_pa_pattern_process_overrides(junk, nozzle_diameter=0.4) == junk
    assert apply_pa_tower_filament_overrides(junk) == junk


def test_pa_line_process_overrides_pin_wall_and_shell_zero():
    """PA Line cube placeholder must print as a 1-wall corner anchor —
    no shells, no infill, no skirt."""
    preset = json.dumps({"wall_loops": "4", "top_shell_layers": "4", "sparse_infill_density": "40%"})
    out = json.loads(apply_pa_line_process_overrides(preset, nozzle_diameter=0.4))
    assert out["wall_loops"] == "1"
    assert out["top_shell_layers"] == "0"
    assert out["bottom_shell_layers"] == "0"
    assert out["sparse_infill_density"] == "0%"
    assert out["skirt_loops"] == "0"
    assert out["brim_type"] == "no_brim"
    assert out["enable_wrapping_detection"] == "0"
    assert out["initial_layer_print_height"] == "0.2"
    assert out["layer_height"] == "0.2"


def test_pa_line_process_overrides_line_width_scales_with_nozzle():
    out = json.loads(apply_pa_line_process_overrides("{}", nozzle_diameter=0.6))
    assert out["line_width"] == "0.6750"
    assert out["initial_layer_line_width"] == "0.8400"


def test_pa_line_filament_overrides_disable_retract_wipe_on_layer_change():
    preset = json.dumps({"filament_retract_when_changing_layer": ["1"], "filament_wipe": ["1"]})
    out = json.loads(apply_pa_line_filament_overrides(preset))
    assert out["filament_retract_when_changing_layer"] == ["0"]
    assert out["filament_wipe"] == ["0"]


def test_pa_line_printer_overrides_disable_wipe_retract_resonance():
    preset = json.dumps({"wipe": ["1"], "retract_when_changing_layer": ["1"], "resonance_avoidance": "1"})
    out = json.loads(apply_pa_line_printer_overrides(preset))
    assert out["wipe"] == ["0"]
    assert out["retract_when_changing_layer"] == ["0"]
    assert out["resonance_avoidance"] == "0"


def test_pa_line_overrides_passthrough_invalid_json():
    junk = "not really json"
    assert apply_pa_line_process_overrides(junk, nozzle_diameter=0.4) == junk
    assert apply_pa_line_filament_overrides(junk) == junk
    assert apply_pa_line_printer_overrides(junk) == junk


def test_pa_pattern_process_overrides_preserve_existing_unrelated_keys():
    """Sidecar gets the full preset, not just our patched keys.
    Anything outside our hardcode list must round-trip intact."""
    preset = json.dumps(
        {
            "name": "0.20mm Standard @BBL A1M",
            "from": "system",
            "type": "process",
            "wall_loops": "4",
            "top_shell_layers": "4",
            "bottom_shell_layers": "4",
            "sparse_infill_density": "40%",
        }
    )
    out = json.loads(apply_pa_pattern_process_overrides(preset, nozzle_diameter=0.4))
    assert out["name"] == "0.20mm Standard @BBL A1M"
    assert out["from"] == "system"
    assert out["type"] == "process"
    # We don't pin top/bottom shells or infill — preset's defaults
    # keep the cube solid (which is what we want for PA Pattern).
    assert out["top_shell_layers"] == "4"
    assert out["bottom_shell_layers"] == "4"
    assert out["sparse_infill_density"] == "40%"
    # But our patched keys win.
    assert out["wall_loops"] == "3"


def test_flow_rate_process_overrides_pin_nozzle_derived_layer_height():
    """Flow Rate pins layer_height = nozzle/2 and the BS process keys."""
    from backend.app.services.calib_preset_overrides import apply_flow_rate_process_overrides

    preset = json.dumps({"layer_height": "0.16", "enable_support": "1", "enable_wrapping_detection": "1"})
    out = json.loads(apply_flow_rate_process_overrides(preset, nozzle_diameter=0.4))
    assert out["layer_height"] == "0.2"
    assert out["initial_layer_print_height"] == "0.2"
    assert out["reduce_crossing_wall"] == "1"
    assert out["enable_wrapping_detection"] == "0"
    assert out["enable_support"] == "0"

    out6 = json.loads(apply_flow_rate_process_overrides("{}", nozzle_diameter=0.6))
    assert out6["layer_height"] == "0.3"
    assert out6["initial_layer_print_height"] == "0.3"


def test_flow_rate_filament_overrides_pin_filament_flow_ratio_scalar():
    """When the preset stores filament_flow_ratio as a scalar string, the
    override replaces it in place."""
    from backend.app.services.calib_preset_overrides import apply_flow_rate_filament_overrides

    preset = json.dumps({"filament_flow_ratio": "0.95", "filament_type": ["PETG"]})
    out = json.loads(apply_flow_rate_filament_overrides(preset, baseline_ratio=1.0))
    assert out["filament_flow_ratio"] == "1"
    # Unrelated keys round-trip intact.
    assert out["filament_type"] == ["PETG"]


def test_flow_rate_filament_overrides_pin_filament_flow_ratio_list():
    """Per-extruder list shape: every entry gets the new baseline."""
    from backend.app.services.calib_preset_overrides import apply_flow_rate_filament_overrides

    preset = json.dumps({"filament_flow_ratio": ["0.95", "0.95"]})
    out = json.loads(apply_flow_rate_filament_overrides(preset, baseline_ratio=0.9975))
    assert out["filament_flow_ratio"] == ["0.9975", "0.9975"]
