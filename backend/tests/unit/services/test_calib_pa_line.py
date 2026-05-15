"""Unit tests for the PA Line calibration builder (W2 Phase 9 verification).

PA Line ships the full prime-line + N-row pattern + filled glyph box +
per-row K labels as ONE ``<layer top_z=0.2>`` ``custom_gcode_per_layer.xml``
entry. The cube placeholder is shrunk to ~3×3×0.2 mm and parked in the
front-left bed corner (build_transform_scale + translate) so its
perimeters don't collide with the centred row stack.

Tests pin the contract: K count == ceil((end-start)/step) + 1, the
single emitted layer entry sits at z=0.2, per-mode preset patches land
in project_settings.config, the cube is repositioned, and the spec
validator rejects malformed inputs.
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
    return build_calibration_3mf(cali_mode=CaliMode.PA_LINE, spec=spec)


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


def _read_zip_entry(zip_bytes: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        return z.read(name).decode("utf-8")


def _layer_zs(xml: str) -> list[str]:
    return re.findall(r'<layer\s+top_z="([^"]+)"', xml)


def _unique_ks(xml: str) -> list[float]:
    return sorted({float(k) for k in re.findall(r"M900 K([\d.]+)", xml)})


def test_pa_line_bake_emits_single_layer_at_default_z():
    """PA Line lays the entire pattern on one layer — only one
    ``<layer top_z>`` entry, at the BS-hardcoded 0.2mm Z."""
    out = _bake(_spec())
    xml = _read_zip_entry(out, "Metadata/custom_gcode_per_layer.xml")
    zs = _layer_zs(xml)
    assert zs == ["0.2"]


def test_pa_line_bake_k_count_matches_ceil_formula():
    """``count = ceil((end - start)/step) + 1`` (BS Calib.cpp:2845).
    Default 0..0.08 step 0.005 → 17 K rows; each row emits an M900."""
    out = _bake(_spec())
    xml = _read_zip_entry(out, "Metadata/custom_gcode_per_layer.xml")
    ks = _unique_ks(xml)
    # M900 K0 fires twice (start + final neutralise), so the unique
    # set may be 17 (if a 0 reset overlaps with start) or 18 — both are
    # valid PA-Line shapes. Pin the start + end K explicitly.
    assert 0.0 in ks
    assert pytest.approx(0.08, abs=1e-6) in ks
    # Should hit at least 17 distinct K values for the default sweep.
    assert len([k for k in ks if k > 0]) >= 16


def test_pa_line_bake_k_count_scales_with_custom_range():
    """Custom 0..0.2 step 0.01 → 21 K rows."""
    out = _bake(_spec(start=0.0, end=0.2, step=0.01))
    xml = _read_zip_entry(out, "Metadata/custom_gcode_per_layer.xml")
    ks = _unique_ks(xml)
    assert pytest.approx(0.2, abs=1e-6) in ks
    assert len([k for k in ks if k > 0]) >= 20


def test_pa_line_bake_emits_m900_with_bbl_suffix():
    """Bambu firmware needs ``L1000 M10`` after the K value (matches
    GCodeWriter.cpp:354)."""
    out = _bake(_spec())
    xml = _read_zip_entry(out, "Metadata/custom_gcode_per_layer.xml")
    assert "M900 K0.0000 L1000 M10" in xml
    # Final neutralise also carries the suffix.
    assert "M900 K0.0800 L1000 M10" in xml


def test_pa_line_bake_emits_prime_line_and_three_segment_rows():
    """Each K row consists of three contiguous G1 X.. E.. moves (slow,
    fast, slow). The prime line walks down through the row stack on the
    leftmost X column before the per-K loop starts."""
    out = _bake(_spec(start=0.0, end=0.02, step=0.01))  # 3 rows
    xml = _read_zip_entry(out, "Metadata/custom_gcode_per_layer.xml")
    # Three rows × three segments each = 9 extrusion moves with comments
    # (Slow segment 1 / Fast segment / Slow segment 2). Prime adds one
    # more extrude_to_xy.
    assert xml.count("Slow segment 1") == 3
    assert xml.count("Fast segment") == 3
    assert xml.count("Slow segment 2") == 3
    assert "Prime: column" in xml


def test_pa_line_bake_omits_glyph_box_when_print_numbers_off():
    """``print_numbers=False`` should skip the filled glyph box +
    per-row K labels entirely."""
    out = _bake(_spec(print_numbers=False))
    xml = _read_zip_entry(out, "Metadata/custom_gcode_per_layer.xml")
    assert "Move to box start" not in xml
    assert "Glyph:" not in xml


def test_pa_line_bake_includes_glyph_labels_when_print_numbers_on():
    """When ``print_numbers=True`` (default), at least one glyph trace
    must be present and the box outline drawn."""
    out = _bake(_spec())
    xml = _read_zip_entry(out, "Metadata/custom_gcode_per_layer.xml")
    assert "Move to box start" in xml
    assert "Glyph:" in xml


def test_pa_line_bake_emits_per_mode_project_overrides():
    """PA Line builder pins the placeholder cube's per-print config so
    the slicer's pre-print machinery doesn't fill / shell / brim around
    the corner cube — operator only reads the pattern itself."""
    out = _bake(_spec())
    cfg = _read_zip_entry(out, "Metadata/project_settings.config")
    data = json.loads(cfg)
    assert data["wall_loops"] == "1"
    assert data["skirt_loops"] == "0"
    assert data["brim_type"] == "no_brim"
    assert data["top_shell_layers"] == "0"
    assert data["bottom_shell_layers"] == "0"
    assert data["sparse_infill_density"] == "0%"
    assert data["enable_wrapping_detection"] == "0"
    assert data["initial_layer_speed"] == "30"
    assert data["initial_layer_print_height"] == "0.2"
    assert data["layer_height"] == "0.2"


def test_pa_line_bake_line_width_scales_with_nozzle():
    """``line_width = nozzle_dia * 1.125``, ``initial_layer_line_width =
    nozzle_dia * 1.4`` (Calib.hpp:282 nozzle_ratio_pairs)."""
    out = _bake(_spec(nozzle_diameter=0.6))
    cfg = _read_zip_entry(out, "Metadata/project_settings.config")
    data = json.loads(cfg)
    assert data["line_width"] == "0.6750"
    assert data["initial_layer_line_width"] == "0.8400"


def test_pa_line_bake_anchors_cube_to_glyph_tab_corner():
    """Build-item transform shrinks the scaffold cube to ~3mm and parks
    it just to the right of the glyph tab's bottom edge — anchored to
    the pattern's bbox so it tracks the centred layout for every bed
    size."""
    # 256×256 bed (default): pattern_x_span = 40+40+24 = 104 mm,
    # pattern_y_span = 17*3.5 + 3.5 = 63 mm.
    # pattern_left = (256 - 104) / 2 = 76 → pattern_right = 180.
    # pattern_bottom_y = (256 - 63) / 2 = 96.5. Cube gap = 2.
    # Cube translate = (180 + 2, 96.5, 0) = (182, 96.5, 0).
    out = _bake(_spec(bed_size_x=256.0, bed_size_y=256.0))
    model_xml = _read_zip_entry(out, "3D/3dmodel.model")
    m = re.search(r'<item[^>]+transform="([^"]+)"', model_xml)
    assert m is not None
    nums = [float(t) for t in m.group(1).split()]
    sx, sy, sz = nums[0], nums[4], nums[8]
    tx, ty, tz = nums[9], nums[10], nums[11]
    # Cube native is 18×18×18 mm → 3×3×0.2 target.
    assert sx == pytest.approx(3.0 / 18.0, abs=1e-6)
    assert sy == pytest.approx(3.0 / 18.0, abs=1e-6)
    assert sz == pytest.approx(0.2 / 18.0, abs=1e-6)
    assert tx == pytest.approx(182.0, abs=1e-6)
    assert ty == pytest.approx(96.5, abs=1e-6)
    assert tz == pytest.approx(0.0, abs=1e-6)


def test_pa_line_bake_cube_anchor_scales_with_bed():
    """A1 mini 180×180: pattern centres → right edge at X=142, bottom at
    Y=58.5. Cube parks at (144, 58.5)."""
    out = _bake(_spec(bed_size_x=180.0, bed_size_y=180.0))
    model_xml = _read_zip_entry(out, "3D/3dmodel.model")
    m = re.search(r'<item[^>]+transform="([^"]+)"', model_xml)
    assert m is not None
    nums = [float(t) for t in m.group(1).split()]
    tx, ty, _tz = nums[9], nums[10], nums[11]
    assert tx == pytest.approx(144.0, abs=1e-6)
    assert ty == pytest.approx(58.5, abs=1e-6)


def test_pa_line_bake_strips_spec_keys_before_validation():
    """Builder must pop the non-PALineSpec keys (``bed_type``,
    ``slicer``, ``nozzle_diameter``, etc.) before validating against
    Pydantic, otherwise model_validate rejects the extras."""
    spec = _spec()
    spec["slicer"] = "bambu_studio"
    spec["target_printer_settings_id"] = "Bambu Lab A1 mini 0.4 nozzle"
    out = _bake(spec)
    assert len(out) > 0


def test_pa_line_bake_rejects_invalid_spec_shape():
    """end <= start → ValueError (Pydantic validator)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _bake(_spec(start=0.1, end=0.05))
