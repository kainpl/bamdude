"""Per-mode preset patches applied right before sidecar slice.

Mirrors what BS / Orca desktop wizards do — `Plater::_calib_*` mutates
the active preset configs in-memory before calling
`background_slicing_process.start_slicing`. The sidecar receives those
in-memory configs via `--load-settings` and so the patches "stick".

Our pipeline path:

    resolve_preset_ref(cloud/local/standard) → JSON string
        → apply_*_overrides(json_str, ...) → JSON string with mode patches
            → slicer_api.slice_with_profiles(printer_profile_json=..., ...)

Without the override step our patches embedded in the 3MF's
`Metadata/project_settings.config` were getting overridden by the
sidecar's `--load-settings` (operator's preset values won over our
embedded ones — verified: sliced output showed `wall_loops=4`,
`initial_layer_speed=['50']` from preset instead of our pinned `3` /
`30`). Patching at the preset JSON layer puts our values where the
sidecar actually consumes them.

Per-mode hardcodes are pulled verbatim from BS source:
- PA Pattern → `Plater.cpp:_calib_pa_pattern` (lines 12543-12625) +
  `Calib.hpp::SuggestedConfigCalibPAPattern` (lines 281-289)
- PA Tower → `Plater.cpp:_calib_pa_tower` (lines 12803-...)
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _set(d: dict[str, Any], key: str, value: Any) -> None:
    """Overwrite ``d[key]`` and log if a meaningful prior value existed."""
    if key in d and d[key] != value:
        logger.debug("calib override: %s = %r (was %r)", key, value, d[key])
    d[key] = value


def apply_pa_pattern_process_overrides(process_json: str, *, nozzle_diameter: float) -> str:
    """Patch a process-preset JSON with PA Pattern hardcodes.

    Values mirror BS `Plater::_calib_pa_pattern`. Lists like
    `initial_layer_speed` are emitted as `[<str>]` (Bambu preset
    format — ConfigOptionFloatsNullable serializes as a JSON array of
    strings). Singleton numerics emit as strings to match existing
    preset shape. Enums emit as their BS-serialization name (e.g.
    `spRear` → ``"back"`` is PA Tower; PA Pattern uses ``"no_brim"``
    for BrimType and ``"by layer"`` for PrintSequence).
    """
    try:
        data = json.loads(process_json)
    except (ValueError, TypeError):
        logger.warning("apply_pa_pattern_process_overrides: input not valid JSON; passing through")
        return process_json
    if not isinstance(data, dict):
        return process_json

    # SuggestedConfigCalibPAPattern (Calib.hpp:281-289) +
    # `Plater::_calib_pa_pattern` (Plater.cpp:12601-12625).
    _set(data, "wall_loops", "3")
    _set(data, "skirt_loops", "0")
    _set(data, "brim_type", "no_brim")
    _set(data, "enable_wrapping_detection", "0")
    _set(data, "print_sequence", "by layer")
    # FloatsNullable list-typed values — keep shape `[str]`.
    _set(data, "initial_layer_speed", ["30"])
    # Per-nozzle line widths — BS uses ConfigOptionFloatOrPercent with
    # the absolute mm value (not percent). Emit as a bare string number.
    _set(data, "line_width", f"{nozzle_diameter * 1.125:.4f}")
    _set(data, "initial_layer_line_width", f"{nozzle_diameter * 1.4:.4f}")
    # Initial / overall layer heights — `SuggestedConfigCalibPAPattern.float_pairs`
    # (Calib.hpp:283). Keep BS-default 0.25/0.2 so pattern math anchors
    # to the same Z planes as the generator computes.
    _set(data, "initial_layer_print_height", "0.25")
    _set(data, "layer_height", "0.2")

    return json.dumps(data)


def apply_pa_pattern_filament_overrides(filament_json: str) -> str:
    """Patch a filament-preset JSON with PA Pattern hardcodes
    (`Plater.cpp:12553-12554`). Retract/wipe on layer change would
    smear the pattern's first-layer G1 trail."""
    try:
        data = json.loads(filament_json)
    except (ValueError, TypeError):
        return filament_json
    if not isinstance(data, dict):
        return filament_json

    _set(data, "filament_retract_when_changing_layer", ["0"])
    _set(data, "filament_wipe", ["0"])
    return json.dumps(data)


def apply_pa_pattern_printer_overrides(printer_json: str) -> str:
    """Patch a printer-preset JSON with PA Pattern hardcodes
    (`Plater.cpp:12555-12557`). Wipe-tower / retract-on-layer-change /
    resonance-avoidance all interfere with the pattern's G1 trail."""
    try:
        data = json.loads(printer_json)
    except (ValueError, TypeError):
        return printer_json
    if not isinstance(data, dict):
        return printer_json

    _set(data, "wipe", ["0"])
    _set(data, "retract_when_changing_layer", ["0"])
    _set(data, "resonance_avoidance", "0")
    return json.dumps(data)


def apply_pa_tower_filament_overrides(filament_json: str) -> str:
    """Patch a filament-preset JSON with PA Tower hardcodes
    (`Plater.cpp:12813`). Without `slow_down_layer_time=1` the
    min-layer-time slowdown can mask per-mm M900 K changes by
    stretching individual layers' print time."""
    try:
        data = json.loads(filament_json)
    except (ValueError, TypeError):
        return filament_json
    if not isinstance(data, dict):
        return filament_json

    _set(data, "slow_down_layer_time", ["1"])
    return json.dumps(data)


def apply_pa_line_process_overrides(process_json: str, *, nozzle_diameter: float) -> str:
    """Patch a process-preset JSON with PA Line hardcodes.

    BS's PA Line wizard doesn't run a dedicated ``Plater::_calib_pa_line``
    routine — the engine emits the pattern in place of slicing — so
    there's no upstream override list to port verbatim. We pin a tight
    set of values that make the placeholder cube minimal (one wall, no
    infill / shells) and keep the pattern G1 trail unbroken by
    layer-change scarring (retract/wipe disabled). Mirrors PA Pattern's
    approach.
    """
    try:
        data = json.loads(process_json)
    except (ValueError, TypeError):
        logger.warning("apply_pa_line_process_overrides: input not valid JSON; passing through")
        return process_json
    if not isinstance(data, dict):
        return process_json

    _set(data, "wall_loops", "1")
    _set(data, "skirt_loops", "0")
    _set(data, "brim_type", "no_brim")
    _set(data, "top_shell_layers", "0")
    _set(data, "bottom_shell_layers", "0")
    _set(data, "sparse_infill_density", "0%")
    _set(data, "enable_wrapping_detection", "0")
    _set(data, "print_sequence", "by layer")
    _set(data, "initial_layer_speed", ["30"])
    _set(data, "line_width", f"{nozzle_diameter * 1.125:.4f}")
    _set(data, "initial_layer_line_width", f"{nozzle_diameter * 1.4:.4f}")
    _set(data, "initial_layer_print_height", "0.2")
    _set(data, "layer_height", "0.2")

    return json.dumps(data)


def apply_pa_line_filament_overrides(filament_json: str) -> str:
    """Disable retract/wipe-on-layer-change for PA Line.

    Pattern's slow→fast→slow extrusion sequence is laid down as one
    continuous G1 chain; any retract or wipe move between segments
    would create gaps the operator reads as PA artefacts.
    """
    try:
        data = json.loads(filament_json)
    except (ValueError, TypeError):
        return filament_json
    if not isinstance(data, dict):
        return filament_json

    _set(data, "filament_retract_when_changing_layer", ["0"])
    _set(data, "filament_wipe", ["0"])
    return json.dumps(data)


def apply_pa_line_printer_overrides(printer_json: str) -> str:
    """Mute resonance avoidance + retract/wipe-on-layer-change for PA Line.

    Same rationale as the PA Pattern printer overrides — anything that
    fires between extrusion segments smears the K-band readout.
    """
    try:
        data = json.loads(printer_json)
    except (ValueError, TypeError):
        return printer_json
    if not isinstance(data, dict):
        return printer_json

    _set(data, "wipe", ["0"])
    _set(data, "retract_when_changing_layer", ["0"])
    _set(data, "resonance_avoidance", "0")
    return json.dumps(data)


def apply_pa_tower_process_overrides(process_json: str) -> str:
    """Patch a process-preset JSON with PA Tower hardcodes
    (`Plater.cpp:12812`). `enable_wrapping_detection=0` is the only
    process-level hardcode; per-object overrides (top/bottom shells=0,
    infill=0%, wall_loops=2, seam_position=back, brim_ears, etc.) are
    applied at the per-object metadata layer via the 3MF writer."""
    try:
        data = json.loads(process_json)
    except (ValueError, TypeError):
        return process_json
    if not isinstance(data, dict):
        return process_json

    _set(data, "enable_wrapping_detection", "0")
    return json.dumps(data)
