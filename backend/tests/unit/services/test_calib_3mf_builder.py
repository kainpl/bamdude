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
