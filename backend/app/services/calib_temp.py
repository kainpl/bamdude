"""Temp Tower (nozzle temperature) calibration builder — W2 Phase 3.

Mirrors BS ``Plater::calib_temp`` (`Plater.cpp:17520`). The operator
prints a tower whose nozzle temperature steps **down** 5 °C every 10 mm
band; the band with the cleanest surface / bridging / overhangs gives the
temperature to use.

Unlike VFA / Vol Speed this builder does a genuine **two-plane mesh cut**:
the temperature tower carries embossed per-band numbers, so it cannot be
Z-scaled (that would distort the numbers *and* break the band ↔ ``M104``
height mapping). The native ``temperature_tower.stl`` covers 350 °C at
the bottom band → 180 °C at the top; the two cuts trim it to the
operator's ``[start, end]`` slab, which is then dropped onto the bed.

The per-layer ``M104`` temperature ramp is **NOT** produced here and NOT
produced by the sidecar — BS/Orca apply it engine-side (``GCode.cpp``
``Calib_Temp_Tower`` case) only when the in-memory ``Print::calib_mode``
flag is set, which is never carried in the 3MF. A vanilla CLI / sidecar
slice therefore holds one constant temperature; BamDude re-creates the
ramp by post-patching the sliced g-code — see
``calib_speed_ramp_patcher.patch_temp_tower`` and
``temp/temp-tower-calibration-bs-orca-analysis.md``.
"""

from __future__ import annotations

import io
import logging

from backend.app.schemas.calibration_spec import CalibTempSpec
from backend.app.services.calib_3mf_writer import (
    WRAPPED_OBJECT_ID,
    ObjectOverride,
    write_calibration_3mf,
)
from backend.app.services.calibration_service import CalibAsset

logger = logging.getLogger(__name__)

# BS ``temperature_tower.stl`` baseline: the native tower's bottom band is
# 350 °C, each 10 mm band is 5 °C cooler. See analysis §3 / §6.
_NATIVE_BASELINE_C = 350.0
_TEMP_STEP_C = 5.0
_BAND_MM = 10.0


def temp_tower_cut_bounds(spec: CalibTempSpec) -> tuple[float, float]:
    """Z bounds (mm) of the slab covering the ``[start, end]`` temp range.

    Verbatim from BS ``calib_temp`` (`Plater.cpp:17560-17582`):

    - bottom cut keeps everything above ``round((350-start)/5)`` bands;
    - upper cut keeps everything below ``round((350-end)/5 + 1)`` bands.

    Returns ``(z_low, z_high)`` in native-STL coordinates.
    """
    low_blocks = round((_NATIVE_BASELINE_C - spec.start) / _TEMP_STEP_C)
    high_blocks = round((_NATIVE_BASELINE_C - spec.end) / _TEMP_STEP_C + 1)
    z_low = max(0.0, low_blocks * _BAND_MM)
    z_high = high_blocks * _BAND_MM
    return z_low, z_high


def _cut_temp_slab(stl_bytes: bytes, z_low: float, z_high: float) -> bytes:
    """Two-plane mesh cut: keep the slab ``z ∈ [z_low, z_high]``, drop to bed.

    The temperature tower is not vertically uniform (embossed band
    numbers), so it is genuinely sliced rather than Z-scaled. ``trimesh``
    is already a calibration-pipeline dependency.
    """
    import trimesh

    mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", process=False)
    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(mesh.dump())
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError("temp: STL did not decode to a usable mesh")

    native_z = float(mesh.bounds[1, 2] - mesh.bounds[0, 2])

    # Trim the base — keep z >= z_low (BS bottom cut, KeepUpper).
    if 0.0 < z_low < native_z:
        mesh = mesh.slice_plane([0.0, 0.0, z_low], [0.0, 0.0, 1.0], cap=True)
    # Trim the top — keep z <= z_high (BS upper cut, KeepLower).
    if 0.0 < z_high < native_z:
        mesh = mesh.slice_plane([0.0, 0.0, z_high], [0.0, 0.0, -1.0], cap=True)
    if mesh is None or not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
        raise ValueError(f"temp: mesh cut to [{z_low}, {z_high}] left no geometry")

    # Drop the slab onto the bed (BS ``ensure_on_bed``): translate so the
    # cut bottom sits at z=0. The patcher's temp(z) = start - floor(z/…)·5
    # then lands on the embossed numbers from the bed up.
    mesh.apply_translation([0.0, 0.0, -float(mesh.bounds[0, 2])])
    return mesh.export(file_type="stl")


def build_temp_3mf(asset: CalibAsset, spec_dict: dict) -> bytes:
    """Bake the Temp Tower 3MF.

    ``spec_dict`` carries the opaque route payload; per-mode keys
    (``bed_type``, ``target_printer_settings_id``, ``slicer``) are popped
    before validating the rest as :class:`CalibTempSpec`. ``start`` /
    ``end`` are nozzle temperatures in °C and **descend** (``start > end``).
    """
    bed_type = spec_dict.pop("bed_type", None) if isinstance(spec_dict, dict) else None
    target_printer_settings_id = (
        spec_dict.pop("target_printer_settings_id", None) if isinstance(spec_dict, dict) else None
    )
    if isinstance(spec_dict, dict):
        spec_dict.pop("slicer", None)

    spec = CalibTempSpec.model_validate(spec_dict)
    # BS Temp_Calibration_Dlg rules (calib_dlg.cpp on_start). Raised as
    # plain ValueError so the slice-only route surfaces them as 409.
    if spec.start > 350:
        raise ValueError("temp: start must be <= 350 °C")
    if spec.end < 180:
        raise ValueError("temp: end must be >= 180 °C")
    if spec.start < spec.end + 5:
        raise ValueError("temp: start must be >= end + 5 °C (temperature descends up the tower)")

    if asset.kind != "stl":
        raise ValueError(
            f"Temp Tower expects an STL scaffold, got kind={asset.kind!r}. Check resolve_asset() for TEMP_TOWER."
        )
    stl_raw = asset.path.read_bytes()

    # Two-plane mesh cut to the operator's temp range (analysis §6).
    z_low, z_high = temp_tower_cut_bounds(spec)
    slab_stl = _cut_temp_slab(stl_raw, z_low, z_high)

    start_temp = int(round(spec.start))

    # Per-object overrides — BS ``calib_temp`` values (analysis §4.1).
    object_overrides = [
        ObjectOverride(
            object_id=WRAPPED_OBJECT_ID,
            config={
                "brim_type": "outer_only",
                "brim_width": "5",
                "brim_object_gap": "0",
            },
        ),
    ]

    # Process- + filament- + printer-preset level → one project patch.
    # The per-layer M104 ramp is NOT set here — it is inserted into the
    # sliced g-code afterwards by calib_speed_ramp_patcher.patch_temp_tower.
    project_patch: dict[str, str] = {
        # process preset
        "enable_wrapping_detection": "0",
        # Force-disable supports. BS/Orca don't set this in calib_temp —
        # the temperature tower is self-supporting and their GUI never
        # offers supports for it — but a sidecar CLI slice has no GUI
        # cascade, so an operator process preset with supports enabled
        # makes the slicer wrap the tower in support material. Same lesson
        # as VFA / Vol Speed.
        "enable_support": "0",
        # filament preset — print starts at the hot end; the descending
        # ramp is post-patched per layer.
        "nozzle_temperature_initial_layer": str(start_temp),
        "nozzle_temperature": str(start_temp),
        # printer preset — resonance avoidance would fight the test (Orca).
        "resonance_avoidance": "0",
    }

    return write_calibration_3mf(
        geometry_bytes=slab_stl,
        geometry_kind="stl",
        custom_gcodes=[],
        object_overrides=object_overrides,
        project_settings_patch=project_patch,
        bed_type=bed_type,
        build_transform_scale=(1.0, 1.0, 1.0),
        target_printer_settings_id=target_printer_settings_id,
        output_filename="temp_tower.3mf",
    )
