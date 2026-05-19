"""Tests for calib_3mf_builder — W2 Phase 0 dispatcher."""

from __future__ import annotations

import io
import zipfile

import pytest

from backend.app.services.calib_3mf_builder import build_calibration_3mf
from backend.app.services.calibration_constants import CaliMode

# Modes whose builders have landed. Each phase commit adds its mode here
# in the same diff it registers the builder in ``calib_3mf_builder``.
_REGISTERED: set[CaliMode] = {
    CaliMode.PA_TOWER,  # Phase 1
    CaliMode.PA_PATTERN,  # Phase 2
    CaliMode.PA_LINE,  # Phase 9 (production)
    CaliMode.VOL_SPEED_TOWER,  # Phase 6 (production)
    CaliMode.VFA_TOWER,  # Phase 5 (production)
    CaliMode.TEMP_TOWER,  # Phase 3 (production)
    CaliMode.RETRACTION_TOWER,  # Phase 4 (production)
    CaliMode.FLOW_RATE,  # Phase 7 (verification)
}


@pytest.mark.parametrize(
    "mode",
    [m for m in CaliMode if m not in _REGISTERED],
)
def test_builder_unregistered_modes_raise_not_implemented(mode):
    """Each phase commit registers a real builder for its mode in the
    same diff it flips MODE_STATE to VERIFICATION. Until then, asking
    for a build is a loud failure rather than silently passing through
    the raw asset."""
    with pytest.raises(NotImplementedError, match="MODE_STATE"):
        build_calibration_3mf(cali_mode=mode, spec={})


def test_temp_tower_builds_valid_3mf():
    """The Temp Tower builder two-plane-cuts the tower and bakes a 3MF."""
    out = build_calibration_3mf(cali_mode=CaliMode.TEMP_TOWER, spec={"start": 230, "end": 190})
    assert isinstance(out, bytes) and len(out) > 0
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        names = z.namelist()
    # A baked (un-sliced) 3MF carries the model + project config, no g-code.
    assert any(n.endswith(".model") for n in names)


def test_temp_tower_rejects_ascending_range():
    """Temp descends — start must be >= end + 5 (BS dialog rule)."""
    with pytest.raises(ValueError):
        build_calibration_3mf(cali_mode=CaliMode.TEMP_TOWER, spec={"start": 190, "end": 230})


def test_retraction_tower_builds_valid_3mf():
    """The Retraction Tower builder Z-trims the tower and bakes a 3MF."""
    out = build_calibration_3mf(cali_mode=CaliMode.RETRACTION_TOWER, spec={"start": 0, "end": 2, "step": 0.1})
    assert isinstance(out, bytes) and len(out) > 0
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        names = z.namelist()
    assert any(n.endswith(".model") for n in names)


def test_retraction_tower_rejects_too_wide_sweep():
    """A sweep needing a tower taller than the 80.4 mm scaffold is rejected."""
    with pytest.raises(ValueError):
        # height = 1.4 + (end-start)/step = 1.4 + 100/0.1 = 1001.4 mm.
        build_calibration_3mf(cali_mode=CaliMode.RETRACTION_TOWER, spec={"start": 0, "end": 100, "step": 0.1})


def test_flow_rate_pass1_builds_valid_3mf():
    out = build_calibration_3mf(cali_mode=CaliMode.FLOW_RATE, spec={"nozzle_diameter": 0.4}, pass_n=1)
    assert isinstance(out, bytes) and len(out) > 0
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        names = z.namelist()
    assert any(n.endswith(".model") for n in names)
    # The 3MF carries the synthesised model_settings + the patched project.
    assert "Metadata/model_settings.config" in names
    assert "Metadata/project_settings.config" in names


def test_flow_rate_pass2_builds_valid_3mf():
    out = build_calibration_3mf(cali_mode=CaliMode.FLOW_RATE, spec={"nozzle_diameter": 0.4}, pass_n=2)
    assert isinstance(out, bytes) and len(out) > 0
