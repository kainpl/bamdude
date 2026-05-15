"""PA Line calibration builder (W2 Phase 9 — VERIFICATION).

PA Line is BS's classic Pressure Advance test: a vertical column of K
rows, each row a slow/fast/slow extrusion segment at a stepped K value.
Operators read the cleanest row to derive K. Unlike PA Tower (M900 per
printed layer) and PA Pattern (custom comb + glyph tab), PA Line's
entire visible geometry lives in **one layer of custom g-code** emitted
in place of the slicer's normal extrusion pass.

In BS-desktop the engine bypasses the loaded ``pressure_advance_test.stl``
entirely when ``calib_mode == Calib_PA_Line`` and writes
``CalibPressureAdvanceLine::print_pa_lines`` output directly. Path C
can't reach that branch through the sidecar CLI, so we:

1. Reuse the PA Pattern cube scaffold (``pa_pattern.3mf``) — it gives
   the slicer four ``<layer>`` boundaries and a small cube placeholder.
2. Shrink the cube via ``build_transform_scale`` + park it in a corner
   so its perimeters don't collide with the row stack.
3. Replace the scaffold's pre-baked custom_gcode_per_layer.xml with one
   ``<layer top_z=0.2>`` entry holding the full PA-Line pattern.
4. Apply BS PA Line's preset-level hardcodes via
   ``calib_preset_overrides.apply_pa_line_*`` before sidecar slice.

Operator-visible knobs land in :class:`PALineSpec`; everything else is
read from the active preset chain (filament diameter, flow ratio, bed
size) or defaults to BS-shipped wizard values.
"""

from __future__ import annotations

import json
import logging

from backend.app.schemas.calibration_spec import PALineSpec
from backend.app.services.calib_3mf_writer import (
    CustomGcodeItem,
    ObjectOverride,
    write_calibration_3mf,
)
from backend.app.services.calib_pa_line_generator import (
    GLYPH_BOX_WIDTH_MM,
    HEIGHT_LAYER,
    LENGTH_LONG_BASE,
    LENGTH_SHORT,
    SPACE_Y,
    PALineParams,
    generate_pa_line_layer,
    num_lines_for_range,
)
from backend.app.services.calibration_service import CalibAsset

logger = logging.getLogger(__name__)


def build_pa_line_3mf(asset: CalibAsset, spec_dict: dict) -> bytes:
    """End-to-end: read scaffold ``pa_pattern.3mf`` → emit a one-layer
    ``custom_gcode_per_layer.xml`` carrying the PA Line pattern → overlay
    PA Line preset hardcodes → return a sliceable 3MF byte string.

    ``spec_dict`` is validated as :class:`PALineSpec`. Extras consumed
    here (popped before validation):

    - ``bed_type`` (str, optional): per-job plate name.
    - ``target_printer_settings_id`` (str, optional): named printer
      preset that the bundle should align to.
    - ``slicer`` (str, optional): informational — both BS + Orca consume
      the same custom-gcode format.
    - ``nozzle_diameter`` (float, default 0.4): nozzle for e-per-mm math.
    - ``filament_diameter`` (float, default 1.75): filament for
      e-per-mm math.
    - ``filament_flow_ratio`` (float, default 1.0): per-preset flow
      ratio.
    - ``bed_size_x`` / ``bed_size_y`` (float, default 256.0 each): bed
      bbox in mm. Pattern centres inside this rectangle.
    - ``fast_speed_mm_s`` / ``slow_speed_mm_s`` (float): mirror BS's
      derived speeds (``outer_wall_speed`` / 4). Default fallbacks
      match the BS PA Line wizard.
    """
    bed_type = spec_dict.pop("bed_type", None) if isinstance(spec_dict, dict) else None
    target_printer_settings_id = (
        spec_dict.pop("target_printer_settings_id", None) if isinstance(spec_dict, dict) else None
    )
    spec_dict.pop("slicer", None) if isinstance(spec_dict, dict) else None

    nozzle_diameter = float(spec_dict.pop("nozzle_diameter", 0.4)) if isinstance(spec_dict, dict) else 0.4
    filament_diameter = float(spec_dict.pop("filament_diameter", 1.75)) if isinstance(spec_dict, dict) else 1.75
    filament_flow_ratio = float(spec_dict.pop("filament_flow_ratio", 1.0)) if isinstance(spec_dict, dict) else 1.0

    bed_size_x = float(spec_dict.pop("bed_size_x", 256.0)) if isinstance(spec_dict, dict) else 256.0
    bed_size_y = float(spec_dict.pop("bed_size_y", 256.0)) if isinstance(spec_dict, dict) else 256.0
    bed_origin_x = float(spec_dict.pop("bed_origin_x", 0.0)) if isinstance(spec_dict, dict) else 0.0
    bed_origin_y = float(spec_dict.pop("bed_origin_y", 0.0)) if isinstance(spec_dict, dict) else 0.0

    fast_speed_mm_s = float(spec_dict.pop("fast_speed_mm_s", 100.0)) if isinstance(spec_dict, dict) else 100.0
    slow_speed_mm_s = float(spec_dict.pop("slow_speed_mm_s", 25.0)) if isinstance(spec_dict, dict) else 25.0

    spec = PALineSpec.model_validate(spec_dict)

    if asset.kind != "3mf":
        raise ValueError(
            f"PA Line expects a 3MF scaffold (shared pa_pattern.3mf), "
            f"got kind={asset.kind!r}. Check resolve_asset() for CaliMode.PA_LINE."
        )
    base_bytes = asset.path.read_bytes()

    count = num_lines_for_range(spec.start, spec.end, spec.step)

    pattern_gcode = generate_pa_line_layer(
        PALineParams(
            start_pa=spec.start,
            step_pa=spec.step,
            count=count,
            nozzle_diameter=nozzle_diameter,
            filament_diameter=filament_diameter,
            filament_flow_ratio=filament_flow_ratio,
            bed_size_x=bed_size_x,
            bed_size_y=bed_size_y,
            bed_origin_x=bed_origin_x,
            bed_origin_y=bed_origin_y,
            fast_speed_mm_s=fast_speed_mm_s,
            slow_speed_mm_s=slow_speed_mm_s,
            draw_numbers=spec.print_numbers,
            is_bbl=True,
        )
    )

    # Single ``<layer top_z=HEIGHT_LAYER>`` entry — BS engine emits the
    # pattern as start_gcode-like prelude on the first layer change, we
    # ride the same boundary via custom_gcode.
    custom_gcodes = [
        CustomGcodeItem(
            print_z=HEIGHT_LAYER,
            extra=pattern_gcode,
            type="Custom",
        )
    ]

    # Cube placeholder: BS-shipped pa_pattern scaffold's cube is 18×18×18 mm,
    # which we shrink to 3×3×0.2 mm. 3 mm is wide enough for the slicer to
    # lay one perimeter at any nozzle ≤ 0.6 mm without the CLI rejecting
    # the geometry, narrow enough to be visually inert.
    cube_native_size = 18.0
    cube_target_xy = 3.0
    cube_target_z = HEIGHT_LAYER
    scale_xy = cube_target_xy / cube_native_size
    scale_z = cube_target_z / cube_native_size

    # Park the cube just to the right of the glyph tab's bottom edge —
    # always inside the bed bbox for the centred pattern, never collides
    # with row segments / labels. The pattern bbox math here mirrors
    # ``generate_pa_line_layer``'s centring; if the generator shifts the
    # layout we recompute the anchor the same way and the cube stays
    # locked to the visible bottom-right corner of the tab.
    length_long = LENGTH_LONG_BASE + min(bed_size_x - 120.0, 0.0)
    pattern_x_span = LENGTH_SHORT * 2.0 + length_long
    if spec.print_numbers:
        pattern_x_span += GLYPH_BOX_WIDTH_MM
    pattern_y_span = count * SPACE_Y + (SPACE_Y if spec.print_numbers else 0.0)

    pattern_left_x = bed_origin_x + (bed_size_x - pattern_x_span) / 2.0
    pattern_right_x = pattern_left_x + pattern_x_span
    pattern_bottom_y = bed_origin_y + (bed_size_y - pattern_y_span) / 2.0

    cube_gap_x = 2.0  # small gap so cube perimeter doesn't touch tab edge
    cube_translate = (
        pattern_right_x + cube_gap_x,
        pattern_bottom_y,
        0.0,
    )

    # Cube stays printable: tried ``printable=false`` + ``extruder=0`` to
    # skip the cube in the slice, but the sidecar then rejects the
    # plate with ``-50 plate is empty / no object fully inside`` (the
    # plate-bbox check counts only printable objects). Leave the tiny
    # 3×3×0.2 mm corner cube in the print — it's cheap and visually
    # off to the side.
    object_overrides: list[ObjectOverride] = []

    logger.debug(
        "build_pa_line_3mf: VERIFICATION, start=%s end=%s step=%s -> %d rows",
        spec.start,
        spec.end,
        spec.step,
        count,
    )

    return write_calibration_3mf(
        geometry_bytes=base_bytes,
        geometry_kind="3mf",
        custom_gcodes=custom_gcodes,
        object_overrides=object_overrides,
        project_settings_patch=_project_settings_patch(nozzle_diameter=nozzle_diameter),
        bed_type=bed_type,
        target_printer_settings_id=target_printer_settings_id,
        build_transform_scale=(scale_xy, scale_xy, scale_z),
        build_transform_translate=cube_translate,
        output_filename="pa_line.3mf",
    )


def _project_settings_patch(*, nozzle_diameter: float) -> dict[str, str]:
    """Project-settings overrides for PA Line.

    BS desktop doesn't run ``Plater::_calib_pa_*`` for PA Line (engine
    handles the pattern directly), so there's no upstream override list
    to port. We pin the operator's preset to settings that keep the
    pattern G1 trail readable:

    - One wall around the placeholder cube; no infill / shells; the
      cube is a corner anchor only.
    - line_width / initial_layer_line_width derived from nozzle so the
      cube's single perimeter is sized identically to BS's PA-Line
      wizard output (visually consistent across nozzle sizes).
    - Disable scarring features (resonance avoidance, wraparound
      detection, retract-between-layer, wipe) — pattern G1 trail is a
      single continuous extrusion sequence and any retract / wipe move
      between segments smears the slow→fast→slow transitions the
      operator reads from.
    """
    return {
        "wall_loops": "1",
        "skirt_loops": "0",
        "brim_type": "no_brim",
        "top_shell_layers": "0",
        "bottom_shell_layers": "0",
        "sparse_infill_density": "0%",
        "line_width": f"{nozzle_diameter * 1.125:.4f}",
        "initial_layer_line_width": f"{nozzle_diameter * 1.4:.4f}",
        "initial_layer_print_height": "0.2",
        "layer_height": "0.2",
        "initial_layer_speed": "30",
        # Mirror PA Pattern's retract/wipe/wrap-detection mute — the
        # pattern's slow→fast→slow extrusion sequence reads cleanest
        # when nothing fires between segments.
        "enable_wrapping_detection": "0",
        "filament_retract_when_changing_layer": "0",
        "filament_wipe": "0",
        "wipe": "0",
        "retract_when_changing_layer": "0",
        "resonance_avoidance": "0",
        # PA Line uses a single contiguous layer so by-layer print
        # sequence matters less than for PA Pattern, but pinning it
        # keeps behaviour predictable.
        "print_sequence": "by layer",
    }


# Fallback bed bbox per printer model — used when the resolved cloud
# preset JSON is a delta (most fields, including ``printable_area``,
# inherit from the unflattened parent). Keys match
# ``backend/app/utils/printer_models.normalize_printer_model`` output.
# All Bambu plates are origin-at-(0,0) rectangles.
_BED_BBOX_BY_MODEL: dict[str, tuple[float, float, float, float]] = {
    # 180 mm plate
    "A1 Mini": (0.0, 0.0, 180.0, 180.0),
    # 256 mm plate (most of the lineup)
    "A1": (0.0, 0.0, 256.0, 256.0),
    "P1P": (0.0, 0.0, 256.0, 256.0),
    "P1S": (0.0, 0.0, 256.0, 256.0),
    "P2S": (0.0, 0.0, 256.0, 256.0),
    "X1": (0.0, 0.0, 256.0, 256.0),
    "X1C": (0.0, 0.0, 256.0, 256.0),
    "X1E": (0.0, 0.0, 256.0, 256.0),
    "X2D": (0.0, 0.0, 256.0, 256.0),
    # 350 × 320 mm plate (H2D family, per BS resources/printers/H2D.json)
    "H2D": (0.0, 0.0, 350.0, 320.0),
    "H2D Pro": (0.0, 0.0, 350.0, 320.0),
    "H2C": (0.0, 0.0, 350.0, 320.0),
    "H2S": (0.0, 0.0, 350.0, 320.0),
}


def bed_bbox_for_model(model: str | None) -> tuple[float, float, float, float] | None:
    """Lookup the bed bbox for a Bambu printer model name.

    Returns ``(origin_x, origin_y, size_x, size_y)`` for known models,
    ``None`` for unknown names so callers can fall back to a default.
    """
    if not model:
        return None
    from backend.app.utils.printer_models import normalize_printer_model

    normalized = normalize_printer_model(model)
    if normalized is None:
        return None
    return _BED_BBOX_BY_MODEL.get(normalized)


def parse_bed_bbox_from_printer_json(printer_json: str) -> tuple[float, float, float, float] | None:
    """Read ``printable_area`` from a resolved printer preset JSON.

    Returns ``(origin_x, origin_y, size_x, size_y)`` of the bed bbox in mm,
    or ``None`` if the field is missing or unparseable. Used by the
    PA Line builder to centre the pattern on the operator's actual plate
    rather than the hardcoded 256×256 default (PA Line generates absolute
    machine coords; without the real bbox the pattern lands off-centre on
    any non-256mm plate — e.g. A1 mini 180×180 ends up in the upper-right
    quadrant).

    BS preset format mirrors ``threemf_capabilities.py:95`` —
    ``printable_area: ["0x0", "256x0", "256x256", "0x256"]``. Polygon is
    assumed rectangular; we take min/max of all vertex coords.
    """
    if not printer_json:
        return None
    try:
        data = json.loads(printer_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    polygon = data.get("printable_area") or []
    if not isinstance(polygon, list) or len(polygon) < 3:
        return None
    xs: list[float] = []
    ys: list[float] = []
    for vertex in polygon:
        if not isinstance(vertex, str) or "x" not in vertex:
            continue
        parts = vertex.split("x")
        if len(parts) != 2:
            continue
        try:
            xs.append(float(parts[0]))
            ys.append(float(parts[1]))
        except ValueError:
            continue
    if len(xs) < 3 or len(ys) < 3:
        return None
    origin_x, origin_y = min(xs), min(ys)
    size_x, size_y = max(xs) - origin_x, max(ys) - origin_y
    if size_x <= 0 or size_y <= 0:
        return None
    return (origin_x, origin_y, size_x, size_y)


__all__ = [
    "build_pa_line_3mf",
    "_project_settings_patch",
    "parse_bed_bbox_from_printer_json",
    "bed_bbox_for_model",
]
