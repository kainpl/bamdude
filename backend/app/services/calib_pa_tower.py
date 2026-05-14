"""PA Tower calibration builder (W2 Phase 1).

Mirrors BS ``Plater::_calib_pa_select`` + ``GCode.cpp:4124-4125``. Given
the user's sweep range (start, end, step), produce:

- A z-cut copy of the scaffold STL trimmed to the minimum height that
  still covers the sweep (no point printing a full 100 mm tower when the
  range is start=0 → end=0.1 step=0.002 → 50 K values across 51 mm).
- A ``Metadata/custom_gcode_per_layer.xml`` body where each printed
  layer carries ``M900 K<value> L1000 M10``. ``K = start + floor(z) *
  step`` (BS clusters the value by 1 mm Z band so all sub-layers in a
  given mm share the same K).
- Per-object override ``seam_position = spRear`` on the tower object so
  the seam doesn't drift through the M900-changing zone.
- Project-settings patch turning off Bambu's wraparound-detection scan,
  which fights the M900 changes between layers.

Operator-visible knobs land in :class:`CalibTowerSpec`; this module
treats it as a validated input and never re-tunes ranges.

Why a per-mode module: tower-style modes share the geometry path
(z_cut + per-Z gcode) but diverge in the specific g-code template
emitted at each layer. Keeping each in its own ``calib_<mode>.py``
keeps the per-mode review surface narrow when each phase lands.
"""

from __future__ import annotations

import logging
import math

from backend.app.schemas.calibration_spec import CalibTowerSpec
from backend.app.services.calib_3mf_writer import (
    WRAPPED_OBJECT_ID,
    CustomGcodeItem,
    ObjectOverride,
    _stl_z_extent,
    write_calibration_3mf,
)
from backend.app.services.calibration_service import CalibAsset

logger = logging.getLogger(__name__)


def pa_tower_height_mm(spec: CalibTowerSpec) -> float:
    """Z height (in mm) the tower needs to cover the full K sweep.

    Per BS ``Plater.cpp:_calib_pa_select`` — ``ceil((end - start) / step)
    + 1``. The +1 gives the slicer one full 1 mm band above the last K
    value so the final layer prints cleanly without being clipped by the
    z-cut plane.
    """
    span = spec.end - spec.start
    bands = math.ceil(span / spec.step)
    return float(bands + 1)


def pa_tower_layer_zs(spec: CalibTowerSpec) -> list[float]:
    """Per-layer Z positions where the slicer should inject M900.

    Iterates ``layer_height, 2*layer_height, ... ≤ tower_height``. First
    layer (z = layer_height) is included; the tower's z-cut height is
    inclusive on the upper end. ``layer_height`` ≤ 0 is rejected by the
    spec validator so we can assume positive here.
    """
    h = pa_tower_height_mm(spec)
    out: list[float] = []
    z = spec.layer_height
    # Use a small epsilon so that floating-point z slightly past h still
    # lands on the upper boundary if it would round to h.
    while z <= h + 1e-9:
        out.append(round(z, 6))
        z += spec.layer_height
    return out


def pa_tower_k_at_z(spec: CalibTowerSpec, z: float) -> float:
    """K value the printer should apply at layer-height ``z``.

    BS uses ``K = start + floor(z) * step`` — sub-layers within the same
    integer mm Z band share a K value, the change fires once per mm at
    the first layer crossing the boundary. We re-emit at every layer
    (matches BS's output) since the slicer's no-op detection makes the
    duplicate M900 lines harmless.
    """
    return spec.start + math.floor(z) * spec.step


def pa_tower_custom_gcodes(spec: CalibTowerSpec) -> list[CustomGcodeItem]:
    """Build the per-Z ``CustomGcodeItem`` list.

    Mirrors BS ``GCode.cpp:4124-4125`` —
    ``M900 K<k:.4f> L1000 M10`` injected at every layer boundary.
    """
    return [
        CustomGcodeItem(
            print_z=z,
            extra=f"M900 K{pa_tower_k_at_z(spec, z):.4f} L1000 M10",
        )
        for z in pa_tower_layer_zs(spec)
    ]


def build_pa_tower_3mf(asset: CalibAsset, spec_dict: dict) -> bytes:
    """End-to-end: load scaffold STL → z-cut → compose calibration 3MF.

    ``spec_dict`` is the opaque dict the route handed to
    :func:`build_calibration_3mf`; we validate it as
    :class:`CalibTowerSpec` here so a malformed spec surfaces as a
    pydantic ValidationError the route maps to 400.

    The ``bed_type`` key in ``spec_dict`` (optional, e.g.
    ``"Textured PEI Plate"``) is passed through to the writer's
    ``project_settings.config`` patch so plate-vs-filament validation
    passes. The writer's own default fallback is filament-permissive
    (Textured PEI Plate); the route's ``body.bed_type`` overrides at
    slice time via the sidecar's ``--curr-bed-type`` CLI flag.
    """
    bed_type = spec_dict.pop("bed_type", None) if isinstance(spec_dict, dict) else None
    target_printer_settings_id = (
        spec_dict.pop("target_printer_settings_id", None) if isinstance(spec_dict, dict) else None
    )
    slicer = spec_dict.pop("slicer", None) if isinstance(spec_dict, dict) else None
    spec = CalibTowerSpec.model_validate(spec_dict)

    if asset.kind != "stl":
        raise ValueError(
            f"PA Tower expects an STL scaffold, got kind={asset.kind!r}. Check resolve_asset() for CaliMode.PA_TOWER."
        )

    stl_raw = asset.path.read_bytes()
    target_height = pa_tower_height_mm(spec)

    # BS itself (``Plater.cpp::_calib_pa_tower`` at line 17346) uses
    # the STL at *native* XY scale and trims Z by mesh-cutting at
    # ``ceil((end-start)/step)+1`` mm via its internal C++ cut routine.
    # We can't easily replicate BS's mesh-cut on the Python side
    # (trimesh's slice_plane needs scipy + shapely + mapbox-earcut
    # for cap-polygon triangulation). Instead we scale Z on the 3MF
    # build-item transform — for a uniform tower geometry the slicer
    # output is functionally identical (the slicer sees the same
    # XY × target_height region either way).
    #
    # ``native_z`` is read from the STL's actual bbox so the same code
    # works for the BS-shipped 80×80×60 mm tower, Orca's 70×70×60 mm
    # variant, or any future replacement — swap the file under the
    # asset path and the build-transform scale recomputes automatically.
    # XY stays 1.0 to match the STL's native wall thickness (its ~1 mm
    # walls are designed for printing at native XY; downscaling makes
    # them thinner than the nozzle and BS rejects with exit -100).
    native_z = _stl_z_extent(stl_raw)
    build_transform_scale = (
        1.0,
        1.0,
        target_height / native_z,
    )

    custom_gcodes = pa_tower_custom_gcodes(spec)
    object_overrides = [
        # Per-object overrides ported 1-to-1 from Orca's own PA Tower
        # calibration project (exported via File → Save Project after
        # running the calibration wizard in Orca-desktop). Orca writes
        # these as ``<metadata>`` entries on ``<object id="2">`` in
        # ``model_settings.config`` — they override the user-preset
        # values during slicing regardless of what the preset says.
        # All values mirror Orca's reference output verbatim so a
        # BamDude PA Tower bake slices identically (modulo printer
        # identity from --load-settings).
        ObjectOverride(
            object_id=WRAPPED_OBJECT_ID,
            config={
                # --- Hollow tower (no shells, no infill, two walls) ---
                # User presets typically have top/bottom_shell=4 and
                # infill ≥ 15% — those fill the tower solid and bury
                # the per-layer M900 K bands under infill. Orca forces
                # all three to 0 / 0 / 0% so the K bands print cleanly
                # on visible wall surfaces. ``wall_loops=2`` keeps the
                # band readable (one outer + one inner perimeter, no
                # smearing across 3-4 wall passes).
                "top_shell_layers": "0",
                "bottom_shell_layers": "0",
                "sparse_infill_density": "0%",
                "wall_loops": "2",
                # ``alternate_extra_wall=0`` disables Orca/BS's per-
                # layer alternating-extra-wall pattern which would
                # otherwise jiggle perimeter count between layers and
                # shift the K-band reference line.
                "alternate_extra_wall": "0",
                # --- Seam position ---
                # ``"back"`` is the JSON/XML serialization of BS's
                # ``spRear`` enum (Plater.cpp:17368). Earlier we tried
                # ``"rear"`` here and the override was silently rejected
                # (BS parsed it as invalid → fell back to the preset's
                # ``aligned`` default). ``seam_slope_type=none`` disables
                # the scarf-seam variant so the seam is a single clean
                # vertical line aligned to the back face, off the gauge
                # face the operator reads K bands from.
                "seam_position": "back",
                "seam_slope_type": "none",
                # --- Brim for adhesion ---
                # Calibration tower is tall + thin → without a brim it
                # risks detaching mid-print and ruining the K-band
                # readout. Orca's PA Tower WIZARD forces ``brim_ears``
                # (flags only at concave corners — minimal material,
                # clean look). BS doesn't implement the ``brim_ears``
                # enum at all (verified empirically — passing it via
                # `--load-settings` produces a 3MF with brim_type=
                # ``no_brim`` and zero ``Brim`` g-code features, the
                # CLI silently drops the unknown enum). For BS we fall
                # back to ``outer_only`` at the same 6 mm width —
                # heavier brim but the only enum BS supports that
                # gives equivalent corner adhesion. Default to Orca's
                # ``brim_ears`` when no slicer is selected (matches
                # historical behaviour).
                **(
                    {"brim_type": "outer_only", "brim_width": "6", "brim_object_gap": "0"}
                    if slicer == "bambu_studio"
                    else {
                        "brim_type": "brim_ears",
                        "brim_width": "6",
                        "brim_object_gap": "0",
                        "brim_ears_max_angle": "135",
                    }
                ),
                # --- Wall speeds ---
                # Both walls at 200 mm/s — Orca's PA Tower wizard sets
                # this so K-band visual density is consistent between
                # outer and inner wall traces (user presets typically
                # run outer slower than inner for surface quality, but
                # for calibration we want equal flow).
                "outer_wall_speed": "200",
                "inner_wall_speed": "200",
            },
        ),
    ]
    project_patch: dict[str, str] = {
        # BS PlaterCalibPA: wraparound detection scans the bed for stray
        # extrusions between layers, which collides with the M900 K change
        # cadence. The cli accepts boolean-as-string.
        # (BS Plater.cpp:17356)
        "enable_wrapping_detection": "0",
        # BS Plater.cpp:17357 forces filament-side slow_down_layer_time
        # to [1] so per-layer M900 K changes aren't shadowed by min-layer-
        # time slowdowns at small layer footprints.
        "slow_down_layer_time": "1",
    }

    return write_calibration_3mf(
        geometry_bytes=stl_raw,
        geometry_kind="stl",
        custom_gcodes=custom_gcodes,
        object_overrides=object_overrides,
        project_settings_patch=project_patch,
        bed_type=bed_type,
        build_transform_scale=build_transform_scale,
        target_printer_settings_id=target_printer_settings_id,
        output_filename="pa_tower.3mf",
    )
