"""Tests for the Flow Rate calibration builder (W2 Phase 7)."""

from __future__ import annotations

import io
import json
import re
import zipfile

import pytest

from backend.app.services.calib_flow_rate import (
    _format_ratio,
    _modifier_from_object_name,
    build_flow_rate_3mf,
)
from backend.app.services.calibration_service import ASSET_ROOT, CalibAsset


def _asset(pass_n: int) -> CalibAsset:
    fname = "flowrate-test-pass2.3mf" if pass_n == 2 else "flowrate-test-pass1.3mf"
    return CalibAsset(ASSET_ROOT / "filament_flow" / fname, "3mf")


# ---------- _modifier_from_object_name (pure) ----------


def test_modifier_from_object_name_handles_positive_negative_zero():
    assert _modifier_from_object_name("flowrate_0") == 0
    assert _modifier_from_object_name("flowrate_5") == 5
    assert _modifier_from_object_name("flowrate_20") == 20
    assert _modifier_from_object_name("flowrate_m5") == -5
    assert _modifier_from_object_name("flowrate_m20") == -20


def test_modifier_from_object_name_rejects_bad_name():
    with pytest.raises(ValueError):
        _modifier_from_object_name("not_a_flowrate_block")
    with pytest.raises(ValueError):
        _modifier_from_object_name("flowrate_")
    with pytest.raises(ValueError):
        _modifier_from_object_name("flowrate_abc")


def test_format_ratio_strips_trailing_zeros():
    # ``:g`` formatting — 1.0 → "1", 1.05 → "1.05", 0.91 → "0.91".
    assert _format_ratio(0) == "1"
    assert _format_ratio(5) == "1.05"
    assert _format_ratio(10) == "1.1"
    assert _format_ratio(20) == "1.2"
    assert _format_ratio(-5) == "0.95"
    assert _format_ratio(-9) == "0.91"
    assert _format_ratio(-20) == "0.8"


# ---------- build_flow_rate_3mf (multi-object per-object override) ----------


def _find_block(ms: str, name: str) -> str:
    # Walk each <object>...</object> block; return the one whose FIRST
    # name-metadata equals ``name`` (lazy-match the inner content so the
    # regex doesn't cross object boundaries).
    for m in re.finditer(r'<object id="\d+">(.*?)</object>', ms, re.DOTALL):
        inner = m.group(1)
        name_m = re.search(r'<metadata key="name" value="([^"]+)"/>', inner)
        if name_m and name_m.group(1) == name:
            return inner
    raise AssertionError(f"object {name!r} missing in model_settings")


def test_build_pass1_bakes_per_object_print_flow_ratio():
    out = build_flow_rate_3mf(_asset(1), {"nozzle_diameter": 0.4})
    assert isinstance(out, bytes) and len(out) > 0
    z = zipfile.ZipFile(io.BytesIO(out))
    ms = z.read("Metadata/model_settings.config").decode()
    expected = {
        "flowrate_m20": "0.8",
        "flowrate_m15": "0.85",
        "flowrate_m10": "0.9",
        "flowrate_m5": "0.95",
        "flowrate_0": "1",
        "flowrate_5": "1.05",
        "flowrate_10": "1.1",
        "flowrate_15": "1.15",
        "flowrate_20": "1.2",
    }
    for name, ratio in expected.items():
        block = _find_block(ms, name)
        assert f'<metadata key="print_flow_ratio" value="{ratio}"/>' in block, (
            f"{name}: expected print_flow_ratio={ratio}"
        )


def test_build_pass2_has_10_downward_modifiers():
    out = build_flow_rate_3mf(_asset(2), {"nozzle_diameter": 0.4})
    z = zipfile.ZipFile(io.BytesIO(out))
    ms = z.read("Metadata/model_settings.config").decode()
    expected = {
        "flowrate_0": "1",
        "flowrate_m1": "0.99",
        "flowrate_m2": "0.98",
        "flowrate_m3": "0.97",
        "flowrate_m4": "0.96",
        "flowrate_m5": "0.95",
        "flowrate_m6": "0.94",
        "flowrate_m7": "0.93",
        "flowrate_m8": "0.92",
        "flowrate_m9": "0.91",
    }
    for name, ratio in expected.items():
        block = _find_block(ms, name)
        assert f'<metadata key="print_flow_ratio" value="{ratio}"/>' in block


def test_build_bakes_process_patch():
    out = build_flow_rate_3mf(_asset(1), {"nozzle_diameter": 0.4})
    z = zipfile.ZipFile(io.BytesIO(out))
    ps = json.loads(z.read("Metadata/project_settings.config"))
    # Process-level: layer_height = nozzle / 2 = 0.2 for a 0.4 nozzle.
    assert ps.get("layer_height") == "0.2"
    assert ps.get("initial_layer_print_height") == "0.2"
    assert ps.get("reduce_crossing_wall") == "1"
    assert ps.get("enable_wrapping_detection") == "0"
    # Sidecar has no GUI cascade — supports off (VFA / Temp / Retraction lesson).
    assert ps.get("enable_support") == "0"


def test_build_rejects_non_3mf_asset():
    bad = CalibAsset(ASSET_ROOT / "filament_flow" / "flowrate-test-pass1.3mf", "stl")
    with pytest.raises(ValueError, match="3MF scaffold"):
        build_flow_rate_3mf(bad, {"nozzle_diameter": 0.4})


def test_build_rejects_non_positive_nozzle():
    with pytest.raises(ValueError, match="nozzle_diameter"):
        build_flow_rate_3mf(_asset(1), {"nozzle_diameter": 0.0})


def test_build_bakes_per_object_geometry_overrides():
    """The 12-key per-object override union from BS Plater.cpp:17456-17486
    (speed vectors deferred — see _PER_OBJECT_BASE_OVERRIDES note)."""
    out = build_flow_rate_3mf(_asset(1), {"nozzle_diameter": 0.4})
    z = zipfile.ZipFile(io.BytesIO(out))
    ms = z.read("Metadata/model_settings.config").decode()
    block = _find_block(ms, "flowrate_0")
    must_have = {
        "wall_loops": "3",
        "top_one_wall_type": "topmost",
        "sparse_infill_density": "35%",
        "top_area_threshold": "100%",
        "bottom_shell_layers": "1",
        "top_shell_layers": "5",
        "detect_thin_wall": "1",
        "filter_out_gap_fill": "0",
        "sparse_infill_pattern": "rectilinear",
        "top_surface_pattern": "monotonic",
        "infill_direction": "45",
        "ironing_type": "no ironing",
        # nozzle * 1.2 = 0.48 for 0.4 nozzle
        "top_surface_line_width": "0.48",
        "internal_solid_infill_line_width": "0.48",
    }
    for k, v in must_have.items():
        assert f'<metadata key="{k}" value="{v}"/>' in block, f"missing override {k}={v}"
