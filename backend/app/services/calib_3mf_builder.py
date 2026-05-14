"""Per-mode calibration 3MF builders (W2 Phase 0+).

Each calibration mode owns a builder function that takes the
mode's spec + the asset geometry and returns a ready-to-slice 3MF byte
string. The slice-only endpoint and the production dispatch path both
call into :func:`build_calibration_3mf` so the per-mode logic stays in
one place.

Phase 0 ships the dispatcher with NotImplementedError stubs for every
mode — the registry in ``calibration_mode_registry`` keeps the same
modes ``DISABLED``, so the dispatcher is unreachable in practice.
Phases 1-7 register the real builders one-by-one in the same commit
that flips ``MODE_STATE[<mode>]`` to ``VERIFICATION``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from backend.app.services.calib_3mf_writer import (
    GeometryKind,
    write_calibration_3mf,
)
from backend.app.services.calib_pa_pattern import build_pa_pattern_3mf
from backend.app.services.calib_pa_tower import build_pa_tower_3mf
from backend.app.services.calibration_constants import CaliMode
from backend.app.services.calibration_service import CalibAsset, resolve_asset

logger = logging.getLogger(__name__)


# Each builder is (asset_path, spec_dict) -> 3mf_bytes. Spec is opaque
# JSON-shaped data; per-mode builders cast / validate against the
# Pydantic specs in ``backend/app/schemas/calibration_spec.py``.
ModeBuilder = Callable[[CalibAsset, dict], bytes]


def _not_implemented(_asset: CalibAsset, _spec: dict) -> bytes:
    raise NotImplementedError(
        "Calibration builder not registered for this mode yet — flip "
        "MODE_STATE[<mode>] from DISABLED to VERIFICATION at the same "
        "time you register the builder in calib_3mf_builder._BUILDERS."
    )


# Source of truth for which mode has a per-mode builder wired up. The
# DISABLED entries here mirror the DISABLED entries in
# ``calibration_mode_registry.MODE_STATE`` — flipping a mode to
# VERIFICATION without also registering its builder will short-circuit
# at runtime with NotImplementedError, which is the right loud failure.
_BUILDERS: dict[CaliMode, ModeBuilder] = {
    # Phase 1 — PRODUCTION (shipped 2026-05-14).
    CaliMode.PA_TOWER: build_pa_tower_3mf,
    # Phase 2 — VERIFICATION (registered builder, awaiting sign-off).
    # Uses BS-shipped pa_pattern.3mf scaffold as-is (K range hardcoded
    # 0..0.08 step 0.005); custom ranges ship in production phase.
    CaliMode.PA_PATTERN: build_pa_pattern_3mf,
    CaliMode.TEMP_TOWER: _not_implemented,
    CaliMode.RETRACTION_TOWER: _not_implemented,
    CaliMode.VFA_TOWER: _not_implemented,
    CaliMode.VOL_SPEED_TOWER: _not_implemented,
    CaliMode.FLOW_RATE: _not_implemented,
    # Auto modes don't slice — they fire MQTT. The dispatcher returning
    # NotImplementedError here is correct: there's nothing to bake.
    CaliMode.AUTO_PA_LINE: _not_implemented,
    CaliMode.PA_LINE: _not_implemented,
}


def build_calibration_3mf(
    *,
    cali_mode: CaliMode,
    spec: dict,
    extruder_count: int = 1,
    pass_n: int = 1,
) -> bytes:
    """Resolve the geometry asset and invoke the per-mode builder.

    Raises:
        NotImplementedError: builder not registered (Phase 0 default).
        ValueError: asset missing on disk.
    """
    asset = resolve_asset(cali_mode, extruder_count=extruder_count, pass_n=pass_n)
    if not asset.path.exists():
        raise ValueError(f"calibration asset not available: {asset.path.name}")

    builder = _BUILDERS.get(cali_mode, _not_implemented)
    return builder(asset, spec)


def passthrough_3mf(asset: CalibAsset) -> bytes:
    """Compose a 3MF directly from the asset with no per-mode injection.

    Used by the Phase 0 smoke test for ``/slice-only``: feed any asset
    through ``write_calibration_3mf`` with empty overrides + empty
    custom-gcode list to verify the routing + sidecar invocation work
    end-to-end. NOT used by production flows — per-mode builders inject
    their own custom-gcode / overrides.
    """
    raw = Path(asset.path).read_bytes()
    if asset.kind == "step":
        raise ValueError("passthrough_3mf does not handle STEP — convert via calib_geometry.step_to_stl first")
    kind: GeometryKind = "3mf" if asset.kind == "3mf" else "stl"
    return write_calibration_3mf(
        geometry_bytes=raw,
        geometry_kind=kind,
        output_filename=asset.path.name,
    )
