"""Unit tests for the PA Pattern calibration builder (W2 Phase 2 production).

The production builder regenerates ``Metadata/custom_gcode_per_layer.xml``
on every call from the operator's ``start/end/step``, via the Python
port of BS ``CalibPressureAdvancePattern::generate_custom_gcodes``. The
shipped ``pa_pattern.3mf`` is used only as scaffold (cube placeholder +
4 layer-change anchors). These tests pin the contract: K count matches
``ceil((end - start)/step + 1)``, the four layer-change print_z are
``[0.25, 0.45, 0.65, 0.85]`` (BS-default initial=0.25 + 3×layer=0.2),
per-mode overrides land in project_settings + model_settings.
"""

from __future__ import annotations

import io
import json
import re
import zipfile

import pytest

from backend.app.services.calib_3mf_builder import build_calibration_3mf
from backend.app.services.calibration_constants import CaliMode


def _bake(spec: dict) -> bytes:
    return build_calibration_3mf(cali_mode=CaliMode.PA_PATTERN, spec=spec)


def _spec(**overrides) -> dict:
    base = {
        "start": 0.0,
        "end": 0.08,
        "step": 0.005,
        "bed_type": "Textured PEI Plate",
        "nozzle_diameter": 0.4,
    }
    base.update(overrides)
    return base


def _unique_ks(xml: str) -> list[float]:
    return sorted({float(k) for k in re.findall(r"M900 K([\d.]+)", xml)})


def _layer_zs(xml: str) -> list[str]:
    return re.findall(r'<layer\s+top_z="([^"]+)"', xml)


def test_pa_pattern_bake_emits_four_layers_with_bs_default_print_z():
    """BS's pattern always emits 4 layer entries — initial_layer (0.25)
    + 3 × layer_height (0.2). Production builder must hit the same
    print_z so the slicer's layer-change machinery splices in at the
    right Z."""
    out = _bake(_spec())
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        xml = z.read("Metadata/custom_gcode_per_layer.xml").decode("utf-8")
    zs = _layer_zs(xml)
    assert zs == ["0.25", "0.45", "0.65", "0.85"]


def test_pa_pattern_bake_k_count_matches_get_num_patterns_for_default_range():
    """``get_num_patterns = ceil((end - start)/step + 1)`` — for the
    BS-default 0..0.08 step 0.005 that's 17 unique K values from
    0.0 to 0.08."""
    out = _bake(_spec())
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        xml = z.read("Metadata/custom_gcode_per_layer.xml").decode("utf-8")
    ks = _unique_ks(xml)
    assert len(ks) == 17
    assert ks[0] == 0.0
    assert ks[-1] == pytest.approx(0.08)


def test_pa_pattern_bake_k_count_scales_with_custom_range():
    """Production builder must regenerate the pattern for arbitrary
    start/end/step — confirming the Python port isn't accidentally
    pinned to the shipped scaffold's hard-coded range."""
    # 0..0.2 step 0.01 = 21 patterns
    out = _bake(_spec(start=0.0, end=0.2, step=0.01))
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        xml = z.read("Metadata/custom_gcode_per_layer.xml").decode("utf-8")
    ks = _unique_ks(xml)
    assert len(ks) == 21
    assert ks[-1] == pytest.approx(0.2)


def test_pa_pattern_bake_k_step_is_consistent_across_sweep():
    out = _bake(_spec(start=0.0, end=0.05, step=0.002))
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        xml = z.read("Metadata/custom_gcode_per_layer.xml").decode("utf-8")
    ks = _unique_ks(xml)
    diffs = [round(ks[i + 1] - ks[i], 6) for i in range(len(ks) - 1)]
    assert all(d == 0.002 for d in diffs), diffs


def test_pa_pattern_bake_emits_per_mode_project_overrides():
    """Plater::_calib_pa_pattern hardcodes a fixed set of project-level
    overrides (Plater.cpp:12601-12625). The builder mirrors them via
    project_settings.config patches so the slicer's run respects them
    regardless of the operator's preset."""
    out = _bake(_spec())
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        ps = json.loads(z.read("Metadata/project_settings.config"))
    assert ps["initial_layer_speed"] == "30"
    assert ps["wall_loops"] == "3"
    assert ps["brim_type"] == "no_brim"
    assert ps["enable_wrapping_detection"] == "0"
    assert ps["skirt_loops"] == "0"
    # SuggestedConfigCalibPAPattern.nozzle_ratio_pairs — nozzle*1.125 for line_width
    assert ps["line_width"] == "0.4500"
    assert ps["initial_layer_line_width"] == "0.5600"


def test_pa_pattern_bake_line_width_scales_with_nozzle():
    """0.6mm nozzle → line_width 0.675, initial 0.84. Pinning these
    asserts the builder reads ``nozzle_diameter`` from the spec extras."""
    out = _bake(_spec(nozzle_diameter=0.6))
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        ps = json.loads(z.read("Metadata/project_settings.config"))
    assert ps["line_width"] == "0.6750"
    assert ps["initial_layer_line_width"] == "0.8400"


def test_pa_pattern_bake_skips_per_object_overrides():
    """PA Pattern's cube is an adhesion base for the pattern's first
    layer (frame + glyph tab printed on top of it). Per-object pins
    that hollow the cube (top_shell=1, bottom_shell=1, infill=0%)
    would leave the pattern's G1 trail with nothing to adhere to.
    Orca's saved project carries zero slice-level per-object
    overrides — preset defaults (typically top/bottom_shells=4,
    infill=40%) make the cube a proper solid base. Mirror that."""
    out = _bake(_spec())
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        ms = z.read("Metadata/model_settings.config").decode("utf-8")
    # No per-object overrides emitted — scaffold's <metadata> entries
    # ("name", "extruder") are scaffold-level, not our additions.
    assert 'key="wall_loops"' not in ms
    assert 'key="brim_type"' not in ms
    assert 'key="seam_position"' not in ms


def test_pa_pattern_bake_repositions_cube_to_upper_left():
    """BS-shipped pa_pattern.3mf parks the cube at bed centre
    (translate (82.79, 86.07)) — squarely on top of the pattern V
    walls. Build-item transform patch moves it to Orca's saved
    position (51.63, 83.5) so the cube prints in upper-left of the
    pattern frame, out of the V walls' way."""
    import re

    out = _bake(_spec())
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        top = z.read("3D/3dmodel.model").decode("utf-8")
    m = re.search(r'<item\b[^>]*?transform="([^"]+)"', top)
    assert m is not None
    parts = m.group(1).split()
    sx, sy, sz = float(parts[0]), float(parts[4]), float(parts[8])
    tx, ty, tz = float(parts[9]), float(parts[10]), float(parts[11])
    # Keep BS-shipped scale (cube stays 5×5×0.85mm)
    assert abs(sx - 0.277777778) < 1e-6
    assert abs(sy - 0.277777778) < 1e-6
    assert abs(sz - 0.0472222222) < 1e-6
    # Translate matches Orca's saved project — upper-left of pattern frame
    assert abs(tx - 51.633841) < 1e-3
    assert abs(ty - 83.5) < 1e-3
    assert abs(tz - 0.4) < 1e-3


def test_pa_pattern_bake_respects_bed_type_override():
    out = _bake(_spec(bed_type="Cool Plate"))
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        ps = json.loads(z.read("Metadata/project_settings.config"))
    assert ps["curr_bed_type"] == "Cool Plate"


def test_pa_pattern_bake_strips_spec_keys_before_validation():
    """The route forwards extras (``bed_type``, ``slicer``,
    ``nozzle_diameter``, ``target_printer_settings_id``,
    ``layer_height``, ``initial_layer_height``) inside ``spec``;
    PAPatternSpec is strict about its declared fields, so the builder
    must pop them out before model_validate."""
    spec = _spec()
    spec["slicer"] = "orcaslicer"
    spec["target_printer_settings_id"] = "Bambu Lab A1 mini 0.4 nozzle"
    spec["layer_height"] = 0.2
    spec["initial_layer_height"] = 0.25
    out = _bake(spec)
    assert len(out) > 0


def test_pa_pattern_bake_rejects_invalid_spec_shape():
    """PAPatternSpec requires ``end > start`` and ``step > 0``."""
    with pytest.raises(ValueError):
        _bake({"start": 0.1, "end": 0.05, "step": 0.005, "nozzle_diameter": 0.4})


def test_pa_pattern_bake_emits_m900_with_bbl_suffix():
    """For Bambu printers the generator must emit ``M900 K<X> L1000
    M10 ; ...`` (GCodeWriter.cpp:354 BBL branch). Without the L1000 M10
    suffix Bambu firmware ignores the M900 silently."""
    out = _bake(_spec())
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        xml = z.read("Metadata/custom_gcode_per_layer.xml").decode("utf-8")
    # Sample a few M900 lines and confirm the BBL suffix
    m900_lines = [line for line in xml.split("&#10;") if line.startswith("M900 K")]
    assert m900_lines, "no M900 lines found in custom_gcode_per_layer.xml"
    assert all("L1000 M10" in line for line in m900_lines[:5])
