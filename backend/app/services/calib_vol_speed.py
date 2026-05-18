"""Volumetric Speed (Max Flowrate) Tower calibration builder — W2 Phase 6.

Mirrors BS ``Plater::calib_max_vol_speed`` (`Plater.cpp:17585`) /
OrcaSlicer (`Plater.cpp:13114`). The operator sweeps volumetric flow
(mm³/s) up a spiral-mode tower; the hotend fails where it can't melt
fast enough, and the failure height back-computes the filament's true
``filament_max_volumetric_speed``.

This module bakes the un-sliced 3MF — geometry (X-fit to bed, Z-trim to
sweep height) + the four-level override set (object / process / filament
/ printer). The **per-layer outer-wall speed ramp is NOT produced here
and NOT produced by the sidecar** — BS/Orca apply it engine-side
(``GCode.cpp`` ``Calib_Vol_speed_Tower`` case) only when the in-memory
``Print::calib_mode`` flag is set, which the GUI sets via
``set_calib_params`` and which is never carried in the 3MF. A vanilla
CLI / sidecar slice therefore yields a flat-speed tower; BamDude
re-creates the ramp by post-patching the sliced g-code — see
``calib_speed_ramp_patcher.py`` and
``temp/vol-speed-calibration-bs-orca-analysis.md`` §7.
"""

from __future__ import annotations

import io
import logging

from backend.app.schemas.calibration_spec import CalibTowerSpec
from backend.app.services.calib_3mf_writer import (
    WRAPPED_OBJECT_ID,
    ObjectOverride,
    write_calibration_3mf,
)
from backend.app.services.calib_geometry import GeometryError, step_to_stl
from backend.app.services.calibration_service import CalibAsset

logger = logging.getLogger(__name__)

# BS / Orca default bed width fallback when the route can't resolve the
# printer's ``printable_area`` (mirrors calib_pa_line's bbox fallback).
_DEFAULT_BED_X_MM = 256.0


def vol_speed_tower_height_mm(spec: CalibTowerSpec) -> float:
    """Z height (mm) the tower needs to cover the volumetric sweep.

    Verbatim from BS/Orca ``calib_max_vol_speed``: ``(end - start + 1) /
    step`` — computed on the *volumetric* (mm³/s) sweep params.
    """
    return (spec.end - spec.start + 1.0) / spec.step


def _stl_xz_extent(stl_bytes: bytes) -> tuple[float, float]:
    """Return (x_extent, z_extent) of an STL mesh in mm."""
    import trimesh

    mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", process=False)
    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(mesh.dump())
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError("vol_speed: STL did not decode to a usable mesh")
    return (
        float(mesh.bounds[1, 0] - mesh.bounds[0, 0]),
        float(mesh.bounds[1, 2] - mesh.bounds[0, 2]),
    )


def build_vol_speed_3mf(asset: CalibAsset, spec_dict: dict) -> bytes:
    """Bake the Volumetric Speed Tower 3MF.

    ``spec_dict`` carries the opaque route payload; per-mode keys
    (``bed_type``, ``target_printer_settings_id``, ``slicer``,
    ``bed_size_x``) are popped before validating the rest as
    :class:`CalibTowerSpec`. For Vol Speed, ``start/end/step`` are in
    **mm³/s** and ``nozzle_diameter`` drives the derived line width /
    layer height (BS overrides the preset's layer height for this mode).
    """
    bed_type = spec_dict.pop("bed_type", None) if isinstance(spec_dict, dict) else None
    target_printer_settings_id = (
        spec_dict.pop("target_printer_settings_id", None) if isinstance(spec_dict, dict) else None
    )
    if isinstance(spec_dict, dict):
        spec_dict.pop("slicer", None)
        bed_x = float(spec_dict.pop("bed_size_x", _DEFAULT_BED_X_MM) or _DEFAULT_BED_X_MM)
    else:
        bed_x = _DEFAULT_BED_X_MM

    spec = CalibTowerSpec.model_validate(spec_dict)
    # BS dialog validation (calib_dlg.cpp:533): start > 0, end >= start + step.
    # CalibTowerSpec already enforces step > 0 and end > start.
    if spec.start <= 0:
        raise ValueError("vol_speed: start volumetric speed must be > 0")
    if spec.end < spec.start + spec.step:
        raise ValueError("vol_speed: end must be >= start + step")

    # resolve_asset() prefers a pre-converted STL when shipped; the STEP
    # is the fallback. trimesh's STEP loader is OpenCascade-backed
    # (``cascadio``) and absent in slim deployments — when only the STEP
    # is available and conversion fails, surface a clear error pointing
    # the operator at the missing pre-converted STL.
    if asset.kind == "stl":
        stl_raw = asset.path.read_bytes()
    elif asset.kind == "step":
        try:
            stl_raw = step_to_stl(asset.path.read_bytes())
        except GeometryError as exc:
            raise ValueError(
                "Vol Speed scaffold STEP→STL conversion is unavailable in this deployment "
                f"({exc}). Ship a pre-converted SpeedTestStructure.stl next to the .step in "
                "backend/app/data/calib_assets/volumetric_speed/."
            ) from exc
    else:
        raise ValueError(
            f"Vol Speed expects an STL or STEP scaffold, got kind={asset.kind!r}. "
            "Check resolve_asset() for VOL_SPEED_TOWER."
        )

    native_x, native_z = _stl_xz_extent(stl_raw)

    # Derived dimensions — BS/Orca calib_max_vol_speed steps 4-5.
    nozzle = spec.nozzle_diameter
    line_width = nozzle * 1.75
    layer_height = nozzle * 0.8

    # XY scale (BS Plater.cpp:17614 / Orca 13131): fit the structure to
    # bed width minus a 10 mm margin. BS only ever scales DOWN
    # (``if scale_obj < 1.0``) and scales X only. The 10 mm margin is
    # exactly the brim allowance: ``brim_width`` is 5 mm and the
    # outer_and_inner brim extends 5 mm past each side, so a tower
    # scaled to ``bed_x - 10`` plus brim lands flush with the bed edge.
    # BS does not subtract the brim separately — the literal -10 is it.
    x_scale = (bed_x - 10.0) / native_x if native_x > 0 else 1.0
    if x_scale >= 1.0:
        x_scale = 1.0

    # Z-trim to the sweep height. BS mesh-cuts (KeepLower); we scale Z on
    # the 3MF build transform instead — the structure is vertically
    # uniform (vase-mode test wall) so the slicer output is functionally
    # identical, and this avoids trimesh's slice_plane CAD-dep chain
    # (same call PA Tower makes — see calib_pa_tower.py).
    target_height = vol_speed_tower_height_mm(spec)
    z_scale = target_height / native_z if native_z > 0 and target_height < native_z else 1.0

    build_transform_scale = (x_scale, 1.0, z_scale)

    # Overrides — by config LEVEL, Orca values (our sign-off reference).
    # See temp/vol-speed-calibration-bs-orca-analysis.md §4.1. The
    # per-layer outer-wall speed ramp is NOT set here — it is rewritten
    # into the sliced g-code afterwards by calib_speed_ramp_patcher.
    object_overrides = [
        ObjectOverride(
            object_id=WRAPPED_OBJECT_ID,
            config={
                # Single-wall hollow spiral-mode tower.
                "wall_loops": "1",
                "top_shell_layers": "0",
                "bottom_shell_layers": "0",
                "sparse_infill_density": "0%",
                "alternate_extra_wall": "0",
                "enable_overhang_speed": "0",
                "precise_z_height": "0",
                # BS/Orca force these from the nozzle for this mode.
                "outer_wall_line_width": f"{line_width:.4f}",
                "layer_height": f"{layer_height:.4f}",
                # Brim for adhesion — Orca's calib_max_vol_speed values.
                "brim_type": "outer_and_inner",
                "brim_width": "5",
                "brim_object_gap": "0",
            },
        ),
    ]

    # Process- + filament- + printer-preset level → one project patch
    # (the 3MF's project_settings.config carries all three categories).
    project_patch: dict[str, str] = {
        # process preset
        "spiral_mode": "1",
        # Spiral/vase mode rejects supports (slicer exit -18) — BS's GUI
        # cascade disables this when spiral_mode flips on; we must too.
        "enable_support": "0",
        "timelapse_type": "0",  # tlTraditional
        "max_volumetric_extrusion_rate_slope": "0",
        "enable_wrapping_detection": "0",
        # filament preset — inflate the cap so it never clips the sweep,
        # and kill min-layer-time slowdowns that would mask the ramp.
        "filament_max_volumetric_speed": "200",
        "slow_down_layer_time": "0",
        # printer preset
        "resonance_avoidance": "0",
    }

    return write_calibration_3mf(
        geometry_bytes=stl_raw,
        geometry_kind="stl",
        custom_gcodes=[],
        object_overrides=object_overrides,
        project_settings_patch=project_patch,
        bed_type=bed_type,
        build_transform_scale=build_transform_scale,
        target_printer_settings_id=target_printer_settings_id,
        output_filename="vol_speed_tower.3mf",
    )
