"""Tests for calib_pa_tower — W2 Phase 1 PA Tower builder."""

from __future__ import annotations

import io
import zipfile

import pytest

from backend.app.schemas.calibration_spec import CalibTowerSpec
from backend.app.services.calib_3mf_builder import build_calibration_3mf
from backend.app.services.calib_pa_tower import (
    pa_tower_custom_gcodes,
    pa_tower_height_mm,
    pa_tower_k_at_z,
    pa_tower_layer_zs,
)
from backend.app.services.calibration_constants import CaliMode

# ---------- Pure formula tests ----------


def test_height_default_pa_range():
    """BS-default PA sweep 0 → 0.1 step 0.002 → 50 K values + 1 mm cap = 51 mm."""
    spec = CalibTowerSpec(start=0.0, end=0.1, step=0.002, layer_height=0.2)
    assert pa_tower_height_mm(spec) == pytest.approx(51.0)


def test_height_short_range():
    """A 20-K-value sweep at step 0.005 lands at 4 mm + 1 = 5 mm."""
    spec = CalibTowerSpec(start=0.0, end=0.02, step=0.005, layer_height=0.2)
    # ceil(0.02 / 0.005) = 4 → 4 + 1 = 5
    assert pa_tower_height_mm(spec) == pytest.approx(5.0)


def test_layer_zs_first_at_layer_height():
    """First entry is exactly ``layer_height`` — slicer's first layer
    boundary. Last entry is the integer multiple ≤ tower_height."""
    spec = CalibTowerSpec(start=0.0, end=0.004, step=0.002, layer_height=0.2)
    # height = ceil(0.004 / 0.002) + 1 = 2 + 1 = 3 mm
    zs = pa_tower_layer_zs(spec)
    assert zs[0] == pytest.approx(0.2)
    assert zs[-1] <= 3.0 + 1e-6
    # 15 layers of 0.2 mm reach exactly 3.0
    assert len(zs) == 15


def test_k_at_z_bands_by_floor_z():
    """K = start + floor(z) * step — sub-1mm layers all share one K."""
    spec = CalibTowerSpec(start=0.0, end=0.1, step=0.002, layer_height=0.2)
    # Layers in the 0-1 mm band → floor(z) = 0
    assert pa_tower_k_at_z(spec, 0.2) == pytest.approx(0.0)
    assert pa_tower_k_at_z(spec, 0.8) == pytest.approx(0.0)
    # First layer of the 1-2 mm band → floor(z) = 1
    assert pa_tower_k_at_z(spec, 1.0) == pytest.approx(0.002)
    # Eighth band → K = 0 + 8 * 0.002 = 0.016
    assert pa_tower_k_at_z(spec, 8.4) == pytest.approx(0.016)


def test_k_at_z_with_offset_start():
    """Non-zero start shifts every K linearly."""
    spec = CalibTowerSpec(start=0.05, end=0.1, step=0.002, layer_height=0.2)
    assert pa_tower_k_at_z(spec, 0.4) == pytest.approx(0.05)
    assert pa_tower_k_at_z(spec, 5.0) == pytest.approx(0.05 + 5 * 0.002)


def test_custom_gcodes_shape():
    spec = CalibTowerSpec(start=0.0, end=0.004, step=0.002, layer_height=0.2)
    items = pa_tower_custom_gcodes(spec)
    # Same count as layer Zs
    assert len(items) == len(pa_tower_layer_zs(spec))
    # Every item carries an M900 K with 4 decimals and BS-format L1000 M10
    for item in items:
        assert "M900 K" in item.extra
        assert " L1000 M10" in item.extra
        assert item.type == "Custom"


def test_custom_gcodes_emit_expected_k_values():
    """Spot-check three Z bands: K should follow start + floor(z)*step."""
    spec = CalibTowerSpec(start=0.0, end=0.1, step=0.002, layer_height=1.0)
    items = pa_tower_custom_gcodes(spec)
    # layer_height = 1.0 → Zs are 1, 2, 3, ... 51
    by_z = {item.print_z: item for item in items}
    assert "M900 K0.0020 " in by_z[1.0].extra  # K at first 1 mm band
    assert "M900 K0.0040 " in by_z[2.0].extra
    assert "M900 K0.0100 " in by_z[5.0].extra


def test_spec_rejects_non_increasing_range():
    """``end > start`` is enforced by the spec validator."""
    with pytest.raises(ValueError):
        CalibTowerSpec(start=0.05, end=0.05, step=0.002, layer_height=0.2)
    with pytest.raises(ValueError):
        CalibTowerSpec(start=0.1, end=0.05, step=0.002, layer_height=0.2)


# ---------- End-to-end build (real scaffold STL) ----------


def test_build_pa_tower_produces_valid_3mf():
    """End-to-end: dispatcher routes PA_TOWER → build_pa_tower_3mf →
    BS-mirrored tower_with_seam.stl (80×80×60 mm, same file BS itself
    uses per Plater.cpp:17346) + pa_pattern.3mf scaffold → composed 3MF."""
    out = build_calibration_3mf(
        cali_mode=CaliMode.PA_TOWER,
        spec={"start": 0.0, "end": 0.02, "step": 0.005, "layer_height": 0.2},
    )
    import json as _json

    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        names = set(z.namelist())
        gcode_xml = z.read("Metadata/custom_gcode_per_layer.xml").decode("utf-8")
        model_xml = z.read("Metadata/model_settings.config").decode("utf-8")
        project_json = _json.loads(z.read("Metadata/project_settings.config").decode("utf-8"))

    # Scaffold boilerplate from pa_pattern.3mf survives
    assert "[Content_Types].xml" in names
    assert "_rels/.rels" in names
    assert "3D/Objects/Cube_1.model" in names
    assert "Metadata/slice_info.config" in names
    # Per-layer M900 emitted
    assert "M900 K" in gcode_xml
    # Per-object seam-back applied ("back" is BS spRear's JSON/XML
    # serialization — Orca's saved PA Tower project writes the same
    # value verbatim)
    assert 'key="seam_position" value="back"' in model_xml
    # Compat list forced empty so the slicer accepts operator's printer
    assert project_json["compatible_printers"] == []
    # Default bed plate is the filament-permissive one
    assert project_json["curr_bed_type"] == "Textured PEI Plate"


def test_build_pa_tower_keeps_native_xy_and_z_scales_to_target():
    """``tower_with_seam.stl`` has ~1 mm walls designed for native
    80×80 print — scaling XY down breaks slicing because the walls
    become thinner than any nozzle. So XY is left at identity (1.0)
    and only Z is scaled to clamp the K-sweep to
    ``pa_tower_height_mm(spec)`` mm (target_height / native 60).
    Mirrors BS's ``Plater.cpp::_calib_pa_tower`` without touching the
    mesh."""
    spec_dict = {"start": 0.0, "end": 0.1, "step": 0.002, "layer_height": 0.2}
    target = pa_tower_height_mm(CalibTowerSpec.model_validate(spec_dict))
    out = build_calibration_3mf(cali_mode=CaliMode.PA_TOWER, spec=spec_dict)
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        top = z.read("3D/3dmodel.model").decode("utf-8")
    expected_z = round(target / 60.0, 4)
    # Transform encodes "sx 0 0 0 sy 0 0 0 sz tx ty tz". XY both 1.0,
    # Z at our scale ratio.
    assert f'"1.0 0 0 0 1.0 0 0 0 {expected_z}' in top or f'"1.0 0 0 0 1.0 0 0 0 {expected_z:.4f}' in top


def test_build_pa_tower_honors_bed_type_override():
    """``spec.bed_type`` flows through PA Tower builder into the
    scaffold's ``curr_bed_type`` so the operator can pick e.g. Cool
    Plate for PLA or Engineering Plate for ABS."""
    import json as _json

    out = build_calibration_3mf(
        cali_mode=CaliMode.PA_TOWER,
        spec={
            "start": 0.0,
            "end": 0.02,
            "step": 0.005,
            "layer_height": 0.2,
            "bed_type": "Engineering Plate",
        },
    )
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        project_json = _json.loads(z.read("Metadata/project_settings.config").decode("utf-8"))
    assert project_json["curr_bed_type"] == "Engineering Plate"


def test_build_pa_tower_count_matches_height():
    """The number of M900 entries in the output matches the layer
    iteration — sanity that pa_tower_custom_gcodes + the writer agreed."""
    spec_dict = {"start": 0.0, "end": 0.04, "step": 0.005, "layer_height": 0.2}
    expected_layers = len(pa_tower_layer_zs(CalibTowerSpec.model_validate(spec_dict)))
    out = build_calibration_3mf(cali_mode=CaliMode.PA_TOWER, spec=spec_dict)
    with zipfile.ZipFile(io.BytesIO(out), "r") as z:
        gcode_xml = z.read("Metadata/custom_gcode_per_layer.xml").decode("utf-8")
    assert gcode_xml.count("M900 K") == expected_layers
