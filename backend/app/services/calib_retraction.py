"""Retraction Tower calibration builder — W2 Phase 4.

Mirrors BS ``Plater::calib_retraction`` (`Plater.cpp:17688`) / OrcaSlicer.
The operator prints a two-pillar tower; each 1 mm of height retracts a
little more, and the height where stringing between the pillars cleans
up gives the retraction length to use.

This module bakes the un-sliced 3MF — geometry (Z-cut to the sweep
height; no XY scaling — the 40 × 15 mm footprint fits every Bambu bed)
+ the override set. The **per-layer retraction-length ramp is NOT
produced here and NOT produced by the sidecar** — BS/Orca apply it
engine-side (``GCode.cpp`` ``Calib_Retraction_tower`` case mutates the
GCodeWriter's ``retraction_length``) only when the in-memory
``Print::calib_mode`` flag is set, which is never carried in the 3MF.

NOTE — this is the **verification-stage** builder: it bakes geometry +
overrides only. The post-slice retraction-rewrite patcher
(``patch_retraction_tower``) is deliberately not wired yet — a vanilla
sidecar slice of this 3MF therefore holds every retraction at the
preset's one constant length, which is exactly the baseline an operator
inspects (raw ``G1 E`` retraction moves) before the patcher is written.
See ``temp/retraction-tower-calibration-bs-orca-analysis.md`` §5/§7.
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
from backend.app.services.calib_geometry import GeometryError, z_cut
from backend.app.services.calibration_service import CalibAsset

logger = logging.getLogger(__name__)


def retraction_tower_height_mm(spec: CalibTowerSpec) -> float:
    """Z height (mm) the tower needs to cover the retraction sweep.

    Verbatim from BS/Orca ``calib_retraction``:
    ``height = 1.0 + 0.4 + (end - start) / step`` — a 1 mm band per
    ``step`` of retraction length, plus a 1.4 mm base.
    """
    return 1.4 + (spec.end - spec.start) / spec.step


def _stl_z_extent(stl_bytes: bytes) -> float:
    """Return the Z extent of an STL mesh in mm."""
    import trimesh

    mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", process=False)
    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(mesh.dump())
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError("retraction: STL did not decode to a usable mesh")
    return float(mesh.bounds[1, 2] - mesh.bounds[0, 2])


def build_retraction_3mf(asset: CalibAsset, spec_dict: dict) -> bytes:
    """Bake the Retraction Tower 3MF.

    ``spec_dict`` carries the opaque route payload; per-mode keys
    (``bed_type``, ``target_printer_settings_id``, ``slicer``) are popped
    before validating the rest as :class:`CalibTowerSpec`. ``start`` /
    ``end`` / ``step`` are retraction lengths in **mm** (ascending).
    """
    bed_type = spec_dict.pop("bed_type", None) if isinstance(spec_dict, dict) else None
    target_printer_settings_id = (
        spec_dict.pop("target_printer_settings_id", None) if isinstance(spec_dict, dict) else None
    )
    if isinstance(spec_dict, dict):
        spec_dict.pop("slicer", None)

    spec = CalibTowerSpec.model_validate(spec_dict)
    # BS/Orca dialog validation (calib_dlg.cpp Retraction_Test_Dlg::on_start):
    # start >= 0, end >= start + step. CalibTowerSpec already enforces
    # step > 0 and end > start.
    if spec.start < 0:
        raise ValueError("retraction: start length must be >= 0 mm")
    if spec.end < spec.start + spec.step:
        raise ValueError("retraction: end must be >= start + step")

    if asset.kind != "stl":
        raise ValueError(
            f"Retraction Tower expects an STL scaffold, got kind={asset.kind!r}. "
            "Check resolve_asset() for RETRACTION_TOWER."
        )
    stl_raw = asset.path.read_bytes()
    native_z = _stl_z_extent(stl_raw)

    # Z-trim to the sweep height. BS mesh-cuts (``cut(KeepLower)`` in
    # Plater::calib_retraction) — we do the same via ``z_cut``. An earlier
    # build_transform Z-scale squashed this non-watertight, non-manifold
    # scaffold so hard the slicer's mesh repair failed (CLI_SLICING_ERROR
    # -100); cutting trims geometry without distorting triangles. Same
    # call Temp Tower makes. No XY scaling — the footprint fits every bed.
    target_height = retraction_tower_height_mm(spec)
    if target_height > native_z:
        raise ValueError(
            f"retraction: sweep needs a {target_height:.1f} mm tower but the scaffold is "
            f"only {native_z:.1f} mm — narrow the range or increase the step."
        )
    try:
        stl_cut = z_cut(stl_raw, target_height)
    except GeometryError as exc:
        raise ValueError(f"retraction: failed to trim scaffold to {target_height:.1f} mm — {exc}") from exc

    # Per-object overrides — the PrintObjectConfig / PrintRegionConfig
    # subset of BS ``Plater::calib_retraction`` (Plater.cpp:17723-17728).
    # BS also sets ``layer_height`` / ``initial_layer_print_height`` on
    # ``obj->config``, but ONLY in memory — its calibration flow slices
    # without ever serialising the project, so those keys never reach a
    # 3MF's model_settings.config. ``initial_layer_print_height`` is a
    # print-level (PrintConfig) key, not a PrintObjectConfig one: baked
    # as per-object metadata it desyncs the object from the plate layer
    # plan (object floats on Z → mid-air extrusion / gaps). Both go in
    # the project patch below instead. The per-layer retraction-length
    # ramp is NOT set here.
    object_overrides = [
        ObjectOverride(
            object_id=WRAPPED_OBJECT_ID,
            config={
                "wall_loops": "2",
                "top_shell_layers": "0",
                "bottom_shell_layers": "3",
                "sparse_infill_density": "0%",
            },
        ),
    ]

    # Process- + printer-preset level → one project patch. BS only sets
    # ``enable_wrapping_detection`` at process level here; the rest are
    # sidecar-CLI necessities (no GUI cascade): supports off so the tower
    # isn't wrapped, firmware retraction off so retraction is slicer-side
    # ``G1 E`` moves the patcher can rewrite, resonance avoidance off so
    # it doesn't fight the test. Also mirrors BS in project_settings.config
    # so a production dispatch (no --load-settings) is self-sufficient.
    project_patch: dict[str, str] = {
        # process preset
        "enable_wrapping_detection": "0",
        "enable_support": "0",
        "layer_height": "0.2",
        "initial_layer_print_height": "0.2",
        # printer preset
        "use_firmware_retraction": "0",
        "resonance_avoidance": "0",
    }

    return write_calibration_3mf(
        geometry_bytes=stl_cut,
        geometry_kind="stl",
        custom_gcodes=[],
        object_overrides=object_overrides,
        project_settings_patch=project_patch,
        bed_type=bed_type,
        target_printer_settings_id=target_printer_settings_id,
        output_filename="retraction_tower.3mf",
    )
