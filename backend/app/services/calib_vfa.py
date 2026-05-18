"""VFA (Vibration Fine Artifacts) Tower calibration builder — W2 Phase 5.

Mirrors BS ``Plater::calib_VFA`` (`Plater.cpp:17747`) / OrcaSlicer
(`Plater.cpp:13247`). The operator sweeps outer-wall speed (mm/s) up a
spiral-mode single-wall tower; the motion system resonates at certain
speeds and leaves periodic surface artefacts, and the failure height
back-computes the speed band to avoid.

This module bakes the un-sliced 3MF — geometry (Z-trim to sweep height;
no XY scaling — the VFA scaffold's ~113 mm footprint fits every Bambu
bed) + the four-level override set. The **per-layer outer-wall speed
ramp is NOT produced here and NOT produced by the sidecar** — BS/Orca
apply it engine-side (``GCode.cpp`` ``Calib_VFA_Tower`` case) only when
the in-memory ``Print::calib_mode`` flag is set, which is never carried
in the 3MF. A vanilla CLI / sidecar slice therefore yields a flat-speed
tower; BamDude re-creates the ramp by post-patching the sliced g-code —
see ``calib_speed_ramp_patcher.patch_vfa_ramp`` and
``temp/vfa-calibration-bs-orca-analysis.md`` §7.
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
from backend.app.services.calibration_service import CalibAsset

logger = logging.getLogger(__name__)


def vfa_tower_height_mm(spec: CalibTowerSpec) -> float:
    """Z height (mm) the tower needs to cover the speed sweep.

    Verbatim from BS/Orca ``calib_VFA``: ``5 · ((end - start) / step + 1)``
    — 5 mm per speed band × band count.
    """
    return 5.0 * ((spec.end - spec.start) / spec.step + 1.0)


def _stl_z_extent(stl_bytes: bytes) -> float:
    """Return the Z extent of an STL mesh in mm."""
    import trimesh

    mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", process=False)
    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(mesh.dump())
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError("vfa: STL did not decode to a usable mesh")
    return float(mesh.bounds[1, 2] - mesh.bounds[0, 2])


def build_vfa_3mf(asset: CalibAsset, spec_dict: dict) -> bytes:
    """Bake the VFA Tower 3MF.

    ``spec_dict`` carries the opaque route payload; per-mode keys
    (``bed_type``, ``target_printer_settings_id``, ``slicer``) are popped
    before validating the rest as :class:`CalibTowerSpec`. For VFA,
    ``start/end/step`` are in **mm/s** — no volumetric transform.
    """
    bed_type = spec_dict.pop("bed_type", None) if isinstance(spec_dict, dict) else None
    target_printer_settings_id = (
        spec_dict.pop("target_printer_settings_id", None) if isinstance(spec_dict, dict) else None
    )
    if isinstance(spec_dict, dict):
        spec_dict.pop("slicer", None)

    spec = CalibTowerSpec.model_validate(spec_dict)
    # BS/Orca dialog validation (calib_dlg.cpp): start > 10, end >= start + step.
    # CalibTowerSpec already enforces step > 0 and end > start.
    if spec.start <= 10:
        raise ValueError("vfa: start speed must be > 10 mm/s")
    if spec.end < spec.start + spec.step:
        raise ValueError("vfa: end must be >= start + step")

    if asset.kind != "stl":
        raise ValueError(f"VFA expects an STL scaffold, got kind={asset.kind!r}. Check resolve_asset() for VFA_TOWER.")
    stl_raw = asset.path.read_bytes()
    native_z = _stl_z_extent(stl_raw)

    # Z-trim to the sweep height. BS mesh-cuts (KeepLower); we scale Z on
    # the 3MF build transform instead — the tower is vertically uniform
    # (spiral-mode test wall) so the slicer output is functionally
    # identical, and this avoids trimesh's slice_plane CAD-dep chain
    # (same call PA Tower / Vol Speed make). No XY scaling — calib_VFA
    # never scales X/Y, the scaffold footprint fits every Bambu bed.
    target_height = vfa_tower_height_mm(spec)
    z_scale = target_height / native_z if native_z > 0 and target_height < native_z else 1.0
    build_transform_scale = (1.0, 1.0, z_scale)

    # Overrides — by config LEVEL, BS+Orca union (see
    # temp/vfa-calibration-bs-orca-analysis.md §4.1). The per-layer
    # outer-wall speed ramp is NOT set here — it is rewritten into the
    # sliced g-code afterwards by calib_speed_ramp_patcher.patch_vfa_ramp.
    object_overrides = [
        ObjectOverride(
            object_id=WRAPPED_OBJECT_ID,
            config={
                # Single-wall hollow spiral-mode tower.
                "wall_loops": "1",
                "top_shell_layers": "0",
                "bottom_shell_layers": "1",
                "sparse_infill_density": "0%",
                "alternate_extra_wall": "0",
                "enable_overhang_speed": "0",
                "precise_z_height": "0",
                "detect_thin_wall": "0",
                # Brim for adhesion — BS/Orca calib_VFA values.
                "brim_type": "outer_only",
                "brim_width": "3",
                "brim_object_gap": "0",
            },
        ),
    ]

    # Process- + filament- + printer-preset level → one project patch
    # (the 3MF's project_settings.config carries all three categories).
    # ``filament_max_volumetric_speed`` is deliberately NOT patched —
    # Orca's calib_VFA leaves it at the filament preset's real value
    # (see calib_preset_overrides.apply_vfa_filament_overrides).
    project_patch: dict[str, str] = {
        # process preset
        "spiral_mode": "1",
        # Spiral/vase mode rejects supports (slicer exit -18) — BS's GUI
        # cascade disables this when spiral_mode flips on; we must too.
        "enable_support": "0",
        "timelapse_type": "0",  # tlTraditional
        "enable_wrapping_detection": "0",
        "enable_height_slowdown": "0",
        "bottom_shell_layers": "1",
        # filament preset — kill min-layer-time slowdowns that mask the ramp.
        "slow_down_layer_time": "0",
        # printer preset — resonance avoidance would fight the VFA test.
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
        output_filename="vfa_tower.3mf",
    )
