"""PA Pattern calibration builder (W2 Phase 2 production).

PA Pattern is the most operator-valuable PA test: a 4-layer flat plate
where the slicer injects fully-drawn comb-and-digit geometry as custom
g-code at each layer change. Unlike PA Tower (where ONLY ``M900 K``
fires per layer, and the geometry is a printed mesh), the pattern's
visible features — concentric boxes, K-value labels, axis titles — are
**all written into ``Metadata/custom_gcode_per_layer.xml`` as raw
``G1`` moves** by BS's ``CalibPressureAdvancePattern::generate_custom_gcodes``
(``Calib.cpp:506``). The shipped ``pa_pattern.3mf`` mesh is just a cube
placeholder that gives the slicer four ``<layer>`` boundaries on which
to inject our pattern.

Production builder:
    Calls :func:`generate_pa_pattern_layers` (Python port of BS
    ``CalibPressureAdvancePattern::generate_custom_gcodes``) with the
    operator's ``start/end/step`` and replaces the scaffold's pre-baked
    ``Metadata/custom_gcode_per_layer.xml`` with the freshly-generated
    one. Project + object overrides come from BS
    ``Plater::_calib_pa_pattern`` (Plater.cpp:12543-12625) — hard-coded
    regardless of preset.

The math constants + glyph drawing + box drawing port lives in
``calib_pa_pattern_generator.py``; this module is the orchestrator that
stitches the generated layers into the 3MF.
"""

from __future__ import annotations

import logging

from backend.app.schemas.calibration_spec import PAPatternSpec
from backend.app.services.calib_3mf_writer import (
    CustomGcodeItem,
    ObjectOverride,
    write_calibration_3mf,
)
from backend.app.services.calib_pa_pattern_generator import (
    PAPatternParams,
    generate_pa_pattern_layers,
)
from backend.app.services.calibration_service import CalibAsset

logger = logging.getLogger(__name__)


def _project_settings_patch(*, nozzle_diameter: float) -> dict[str, str]:
    """Project-settings overrides from BS ``Plater::_calib_pa_pattern``
    (Plater.cpp:12543-12625). Speeds / jerk / accel pulled from the
    operator's preset are not patched here — the sidecar inherits them
    through ``--load-settings`` and the printout matches BS's default
    behaviour for those values. We force only the keys BS hard-codes for
    this mode regardless of preset.
    """
    return {
        # Verbatim from SuggestedConfigCalibPAPattern (Calib.hpp:281-289).
        # nozzle_ratio_pairs in C++ go to ConfigOptionFloatOrPercent with
        # value = nozzle_dia * pct/100 → emit the absolute float (mm)
        # as a bare string, matching how the scaffold's
        # project_settings.config stores other line-width values.
        "initial_layer_speed": "30",
        "line_width": f"{nozzle_diameter * 1.125:.4f}",
        "initial_layer_line_width": f"{nozzle_diameter * 1.4:.4f}",
        "skirt_loops": "0",
        "wall_loops": "3",
        "brim_type": "no_brim",
        # Plater.cpp:12625
        "enable_wrapping_detection": "0",
        # Plater.cpp:12577 — pattern's multi-block design assumes by-layer
        # print sequence (each layer hops between K-blocks).
        "print_sequence": "by layer",
        # Plater.cpp:12553-12557 — retract/wipe between layers would
        # smear the pattern. Filament + printer-side flags both go
        # through project_settings.config.
        "filament_retract_when_changing_layer": "0",
        "filament_wipe": "0",
        "wipe": "0",
        "retract_when_changing_layer": "0",
        # Plater.cpp:12557 — Klipper-style resonance avoidance toggles
        # nozzle accel curves that would distort the K bands.
        "resonance_avoidance": "0",
    }


def build_pa_pattern_3mf(asset: CalibAsset, spec_dict: dict) -> bytes:
    """End-to-end: read scaffold ``pa_pattern.3mf`` → regenerate the
    custom_gcode_per_layer.xml with the operator's K range → overlay
    PA Pattern overrides → return a sliceable 3MF byte string.

    ``spec_dict`` is validated as :class:`PAPatternSpec`. Extras
    consumed here (popped before validation):

    - ``bed_type``: per-job plate name, passed to writer for the
      ``curr_bed_type`` patch on ``project_settings.config``.
    - ``target_printer_settings_id``: when bundle mode is used, name
      the project's printer identity to match the bundle.
    - ``slicer``: informational; both BS + Orca consume the same
      pattern format.
    - ``nozzle_diameter``: drives line_width / initial_layer_line_width
      overrides and the generator's e-per-mm math.
    - ``layer_height`` / ``initial_layer_height``: generator inputs.
    - ``wall_count``, ``speed_*``, ``filament_diameter``,
      ``filament_flow_ratio``: optional, generator defaults match BS
      preset defaults.
    """
    bed_type = spec_dict.pop("bed_type", None) if isinstance(spec_dict, dict) else None
    target_printer_settings_id = (
        spec_dict.pop("target_printer_settings_id", None) if isinstance(spec_dict, dict) else None
    )
    spec_dict.pop("slicer", None) if isinstance(spec_dict, dict) else None
    nozzle_diameter = float(spec_dict.pop("nozzle_diameter", 0.4)) if isinstance(spec_dict, dict) else 0.4
    layer_height = float(spec_dict.pop("layer_height", 0.2)) if isinstance(spec_dict, dict) else 0.2
    initial_layer_height = float(spec_dict.pop("initial_layer_height", 0.25)) if isinstance(spec_dict, dict) else 0.25
    spec = PAPatternSpec.model_validate(spec_dict)

    if asset.kind != "3mf":
        raise ValueError(
            f"PA Pattern expects a 3MF scaffold (BS-shipped pa_pattern.3mf), "
            f"got kind={asset.kind!r}. Check resolve_asset() for CaliMode.PA_PATTERN."
        )
    base_bytes = asset.path.read_bytes()

    # Regenerate the pattern g-code for the operator's K range. The
    # writer.py 3MF pass-through path will replace the scaffold's
    # baked custom_gcode_per_layer.xml when this list is non-empty.
    pattern_layers = generate_pa_pattern_layers(
        PAPatternParams(
            start_pa=spec.start,
            end_pa=spec.end,
            step_pa=spec.step,
            nozzle_diameter=nozzle_diameter,
            layer_height=layer_height,
            initial_layer_height=initial_layer_height,
        )
    )
    custom_gcodes = [CustomGcodeItem(print_z=L.print_z, extra=L.extra, type="Custom") for L in pattern_layers]

    # Orca's saved PA Pattern project carries ZERO per-object overrides
    # for slice output — the cube is treated as a regular small object
    # printed at the preset's defaults (typically top/bottom_shells=4,
    # wall_loops=3, infill=40%). This matters because the cube is the
    # adhesion base under part of the pattern's frame + glyph tab; if
    # we pin top_shell_layers=1 / bottom_shell_layers=1 / infill=0%
    # (as PA Tower needs) the cube turns into a perforated shell and
    # the pattern's first-layer G1 trail has nothing to adhere to.
    # Pass-through is the right call here. PA Tower's hollow-tower
    # geometry is a different shape; we don't share the override list.
    object_overrides: list[ObjectOverride] = []

    logger.debug(
        "build_pa_pattern_3mf: production stage, start=%s end=%s step=%s -> %d patterns, %d layers",
        spec.start,
        spec.end,
        spec.step,
        # ceil((end - start) / step + 1) — matches BS get_num_patterns
        len(pattern_layers),
        len(pattern_layers),
    )

    return write_calibration_3mf(
        geometry_bytes=base_bytes,
        geometry_kind="3mf",
        custom_gcodes=custom_gcodes,
        object_overrides=object_overrides,
        project_settings_patch=_project_settings_patch(nozzle_diameter=nozzle_diameter),
        bed_type=bed_type,
        target_printer_settings_id=target_printer_settings_id,
        # Keep BS-shipped scale (0.278/0.278/0.047 → 5×5×0.85mm cube)
        # but move cube to upper-left of pattern frame so its perimeters
        # don't overprint the pattern V walls / glyph digits. Mirrors
        # what OrcaSlicer's PA Pattern wizard produces in File→Save
        # Project (translate (51.63, 83.5, 0.4) vs BS-shipped's centre
        # (82.79, 86.07, 0.425) which sits squarely on top of the V
        # walls and smears the K bands during print).
        build_transform_scale=(0.277777778, 0.277777778, 0.0472222222),
        build_transform_translate=(51.633841, 83.5, 0.4),
        output_filename="pa_pattern.3mf",
    )
