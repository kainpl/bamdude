"""Flow Rate calibration builder — W2 Phase 7.

Mirrors BS ``Plater::calib_flowrate`` (`Plater.cpp:17410`) — a two-pass
test in which every test block is a separately-named object in the 3MF
and the slicer honours a per-object ``print_flow_ratio`` override. No
engine per-layer ramp (no ``Calib_Flow_Rate`` case in ``GCode.cpp``),
so no post-slice patcher.

Pass 1 — coarse, 9 blocks ``{-20..+20}`` step 5 percent.
Pass 2 — fine, 10 blocks ``{-9..0}`` step 1 percent (downward-only
refinement on top of the coarse-picked ``filament_flow_ratio``).

See ``temp/flow-rate-calibration-bs-orca-analysis.md`` and
``docs/superpowers/specs/2026-05-20-flow-rate-calibration-design.md``.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile

from backend.app.services.calib_3mf_writer import ObjectOverride, write_calibration_3mf
from backend.app.services.calibration_service import CalibAsset

logger = logging.getLogger(__name__)

# BS Plater.cpp:17456-17486 — the per-object override union, minus the
# speed-vector clamps (``internal_solid_infill_speed`` / ``top_surface_speed``)
# which BS sources from ``generate_max_speed_parameter_value(...)``. Skipped
# for the verification stage — the test still surfaces over-extrusion to
# the eye regardless; revisit if sign-off shows a discrepancy.
_PER_OBJECT_BASE_OVERRIDES: dict[str, str] = {
    "wall_loops": "3",
    "top_one_wall_type": "topmost",
    "sparse_infill_density": "35%",
    "top_area_threshold": "100%",
    "bottom_shell_layers": "1",
    "top_shell_layers": "5",
    "detect_thin_wall": "1",
    "filter_out_gap_fill": "0",
    "sparse_infill_pattern": "rectilinear",
    "top_surface_pattern": "monotonic",
    "infill_direction": "45",
    "ironing_type": "no ironing",
}


def _modifier_from_object_name(name: str) -> int:
    """Parse the per-block flow-ratio modifier from its object name.

    BS names every block ``flowrate_<mod>``: ``flowrate_0``, ``flowrate_5``,
    ``flowrate_m5`` (= -5%). Mirrors BS ``Plater.cpp:17481-17485``.
    """
    if not name.startswith("flowrate_"):
        raise ValueError(f"unrecognised flow-rate block name {name!r}")
    suffix = name[len("flowrate_") :]
    if not suffix:
        raise ValueError(f"unrecognised flow-rate block name {name!r}")
    if suffix[0] == "m":
        suffix = "-" + suffix[1:]
    try:
        return int(suffix)
    except ValueError as exc:
        raise ValueError(f"unrecognised flow-rate block name {name!r}") from exc


def _format_ratio(mod: int) -> str:
    """``1 + mod/100`` formatted as the slicer writes ratios — ``:g`` so
    ``1.0`` → ``"1"``, ``1.05`` → ``"1.05"``, ``0.91`` → ``"0.91"``."""
    return f"{1.0 + mod / 100.0:g}"


def _scaffold_object_names(threemf_bytes: bytes) -> list[str]:
    """Return the per-block object names declared in the scaffold.

    Bambu's Flow Rate scaffolds are bare-geometry — no
    ``Metadata/model_settings.config``. The names live directly on
    ``<object id="N" name="X">`` inside ``3D/3dmodel.model``, which is
    exactly what BS ``Plater::calib_flowrate`` parses with
    ``model().objects[i]->name``.
    """
    z = zipfile.ZipFile(io.BytesIO(threemf_bytes))
    if "3D/3dmodel.model" not in z.namelist():
        raise ValueError("flow-rate scaffold has no 3D/3dmodel.model")
    top = z.read("3D/3dmodel.model").decode("utf-8", errors="replace")
    return [m.group(2) for m in re.finditer(r'<object\s+id="(\d+)"[^>]*\bname="([^"]+)"', top)]


def build_flow_rate_3mf(asset: CalibAsset, spec_dict: dict) -> bytes:
    """Bake the Flow Rate (pass 1 coarse or pass 2 fine) 3MF.

    Pass is encoded in the asset filename — ``resolve_asset(FLOW_RATE,
    pass_n=...)`` picks ``flowrate-test-pass{1,2}.3mf``; the builder
    treats the two passes identically apart from the scaffold it loads.
    Pass-2's ``filament_flow_ratio`` re-centering is handled by the
    operator preset that arrives through ``--load-settings``.

    ``spec_dict`` may carry ``nozzle_diameter`` (default 0.4),
    ``bed_type``, ``target_printer_settings_id`` — the same shape every
    W2 builder consumes.
    """
    pass_label = "pass2" if "pass2" in asset.path.name else "pass1"
    if asset.kind != "3mf":
        raise ValueError(
            f"Flow Rate expects a 3MF scaffold, got kind={asset.kind!r}. Check resolve_asset() for FLOW_RATE."
        )

    bed_type = spec_dict.pop("bed_type", None) if isinstance(spec_dict, dict) else None
    target_printer_settings_id = (
        spec_dict.pop("target_printer_settings_id", None) if isinstance(spec_dict, dict) else None
    )
    if isinstance(spec_dict, dict):
        spec_dict.pop("slicer", None)
    nozzle_diameter = float((spec_dict or {}).get("nozzle_diameter", 0.4))
    if nozzle_diameter <= 0:
        raise ValueError(f"nozzle_diameter must be positive, got {nozzle_diameter}")

    scaffold_bytes = asset.path.read_bytes()
    block_names = _scaffold_object_names(scaffold_bytes)
    if not block_names:
        raise ValueError("flow-rate scaffold has no named objects")

    layer_height = nozzle_diameter / 2.0
    line_width = nozzle_diameter * 1.2

    object_overrides: list[ObjectOverride] = []
    for name in block_names:
        mod = _modifier_from_object_name(name)
        per_object = dict(_PER_OBJECT_BASE_OVERRIDES)
        per_object["top_surface_line_width"] = f"{line_width:g}"
        per_object["internal_solid_infill_line_width"] = f"{line_width:g}"
        per_object["print_flow_ratio"] = _format_ratio(mod)
        object_overrides.append(ObjectOverride(object_name=name, config=per_object))

    # BS Plater.cpp:17488-17491 + the sidecar-no-GUI-cascade supports lesson.
    project_patch: dict[str, str] = {
        "layer_height": f"{layer_height:g}",
        "initial_layer_print_height": f"{layer_height:g}",
        "reduce_crossing_wall": "1",
        "enable_wrapping_detection": "0",
        "enable_support": "0",
    }

    # BS xy_scale = nozzle / 0.6, applied only when > 1.2; z_scale =
    # (first_layer_height + 6 * layer_height) / 1.4 — see analysis §2.5.
    # first_layer_height = max(preset value, layer_height); we set it to
    # layer_height on the process patch, so first_layer_height == layer_height.
    xy_scale = nozzle_diameter / 0.6
    z_scale = (layer_height + 6 * layer_height) / 1.4
    build_transform_scale: tuple[float, float, float] | None
    if xy_scale > 1.2:
        build_transform_scale = (xy_scale, xy_scale, z_scale)
    elif z_scale != 1.0:
        build_transform_scale = (1.0, 1.0, z_scale)
    else:
        build_transform_scale = None

    return write_calibration_3mf(
        geometry_bytes=scaffold_bytes,
        geometry_kind="3mf",
        custom_gcodes=[],
        object_overrides=object_overrides,
        project_settings_patch=project_patch,
        bed_type=bed_type,
        build_transform_scale=build_transform_scale,
        target_printer_settings_id=target_printer_settings_id,
        output_filename=f"flow_rate_{pass_label}.3mf",
    )
