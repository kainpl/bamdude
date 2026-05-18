"""Volumetric Speed Tower — post-slice per-layer feedrate ramp patcher (W2 Phase 6).

The slicer sidecar does **not** apply the Vol-Speed ramp. BS/Orca produce
it engine-side (Orca ``GCode.cpp:4504`` — per layer
``outer_wall_speed = round(start + z·step)``), but that branch fires only
when ``Print::calib_mode() == Calib_Vol_speed_Tower``. That flag is set
in-memory by the desktop GUI (``set_calib_params``) and is **not carried
in the 3MF**, so a vanilla CLI / sidecar slice has no way to learn it —
verified against two sidecar outputs (Orca + BS backend): every layer
prints at one flat feedrate (200 mm/s), which makes the tower useless
for calibration.

BamDude therefore re-creates the ramp by rewriting the per-layer
feedrate in the sliced g-code, mirroring BS/Orca
``Plater::calib_max_vol_speed`` (Plater.cpp:13180) + the
``Calib_Vol_speed_Tower`` case in ``GCode.cpp``:

    mm3_per_mm = (line_width - layer_height·(1 - π/4))·layer_height · flow_ratio
    speed(z)   = round( start/mm3_per_mm + z·step/mm3_per_mm )      [mm/s]

where ``start``/``step`` are the operator's *volumetric* (mm³/s) sweep
params. In spiral/vase mode the whole layer is one outer-wall spiral, so
each layer carries exactly one bare ``G1 F<feedrate>`` line — that is the
line we rewrite (``F`` is mm/min, hence ``·60``). The first layer keeps
its slicer-assigned first-layer speed (the ramp covers layers 1..N, same
as the desktop reference).

NOTE for the PRODUCTION promotion (registry §5 sign-off): the production
dispatch path slices independently of ``/slice-only`` — it must call
``patch_vol_speed_ramp`` on its sliced 3MF too, or dispatched towers will
print flat.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import re
import zipfile

logger = logging.getLogger(__name__)

_GCODE_ENTRY = "Metadata/plate_1.gcode"
_MD5_ENTRY = "Metadata/plate_1.gcode.md5"
_PROJECT_CONFIG_ENTRY = "Metadata/project_settings.config"

# A layer's print feedrate: a G1 carrying only an F word (no X/Y/Z/E).
# Travel lifts emit "G1 Z.. F.." (has Z) so they never match.
_BARE_F = re.compile(r"^G1 F([\d.]+)\s*$")
_Z_HEIGHT = re.compile(r";\s*Z_HEIGHT:\s*([\d.]+)")


def _flow_mm3_per_mm(line_width: float, layer_height: float, flow_ratio: float) -> float:
    """Slic3r ``Flow::mm3_per_mm`` for a non-bridge extrusion, × flow ratio."""
    return (line_width - layer_height * (1.0 - 0.25 * math.pi)) * layer_height * flow_ratio


def _resolve_flow_ratio(src: zipfile.ZipFile, fallback: float) -> float:
    """Read the slicer-resolved ``filament_flow_ratio`` from the sliced 3MF.

    ``project_settings.config`` carries the *flattened* config the slicer
    actually used — authoritative even when the operator's input filament
    preset was a cloud delta that inherited ``filament_flow_ratio`` from a
    base-material parent (mirrors the ``base_id`` chain walk that
    ``get_filament_info`` does for ``nozzle_temperature``).
    """
    if _PROJECT_CONFIG_ENTRY not in src.namelist():
        return fallback
    try:
        cfg = json.loads(src.read(_PROJECT_CONFIG_ENTRY))
        fr = cfg.get("filament_flow_ratio")
        if isinstance(fr, list):
            fr = fr[0] if fr else None
        return float(fr) if fr is not None else fallback
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return fallback


def _patch_gcode(text: str, start_lin: float, step_lin: float) -> tuple[str, int]:
    """Rewrite the per-layer feedrate. Returns (patched_text, layers_patched)."""
    lines = text.splitlines(keepends=True)
    n = len(lines)
    idx = 0
    block_index = -1
    patched = 0
    while idx < n:
        if lines[idx].startswith("; CHANGE_LAYER"):
            block_index += 1
            z_h: float | None = None
            j = idx + 1
            while j < n and not lines[j].startswith("; CHANGE_LAYER"):
                mz = _Z_HEIGHT.match(lines[j])
                if mz:
                    z_h = float(mz.group(1))
                # First CHANGE_LAYER block is the first printed layer — it
                # carries the brim + first-layer moves at the slicer's
                # first-layer speed; the ramp starts at the next layer.
                if block_index >= 1 and z_h is not None:
                    mf = _BARE_F.match(lines[j].rstrip("\r\n"))
                    if mf:
                        speed = round(start_lin + z_h * step_lin)
                        feedrate = max(1, speed) * 60
                        eol = lines[j][len(lines[j].rstrip("\r\n")) :]
                        lines[j] = f"G1 F{feedrate}{eol}"
                        patched += 1
                j += 1
            idx = j
        else:
            idx += 1
    return "".join(lines), patched


def patch_vol_speed_ramp(
    threemf_bytes: bytes,
    *,
    start: float,
    step: float,
    nozzle_diameter: float,
    flow_ratio: float | None = None,
) -> bytes:
    """Rewrite a sliced Vol-Speed 3MF so each layer ramps the outer-wall feedrate.

    ``start``/``step`` are the volumetric (mm³/s) sweep params. ``end`` is
    not needed — the ramp is open-ended and the tower height already
    bounds it. ``flow_ratio`` is read from the sliced 3MF's resolved
    ``project_settings.config`` when left ``None`` (authoritative — see
    :func:`_resolve_flow_ratio`); pass an explicit value only to override.
    Returns the repacked 3MF bytes (g-code entry rewritten, ``.gcode.md5``
    sidecar recomputed); all other entries are copied verbatim.
    """
    src = zipfile.ZipFile(io.BytesIO(threemf_bytes))
    if _GCODE_ENTRY not in src.namelist():
        raise ValueError(f"vol_speed patcher: 3MF has no {_GCODE_ENTRY} — was export_3mf set?")

    effective_flow = _resolve_flow_ratio(src, flow_ratio if flow_ratio is not None else 1.0)
    line_width = nozzle_diameter * 1.75
    layer_height = nozzle_diameter * 0.8
    mm3_per_mm = _flow_mm3_per_mm(line_width, layer_height, effective_flow)
    if mm3_per_mm <= 0:
        raise ValueError("vol_speed patcher: non-positive mm3_per_mm — bad nozzle/flow params")
    start_lin = start / mm3_per_mm
    step_lin = step / mm3_per_mm

    gcode_text = src.read(_GCODE_ENTRY).decode("utf-8", "replace")
    patched_text, layers = _patch_gcode(gcode_text, start_lin, step_lin)
    if layers == 0:
        raise ValueError(
            "vol_speed patcher: no layer feedrate lines rewritten — sliced g-code "
            "structure not recognised (expected '; CHANGE_LAYER' + bare 'G1 F')."
        )
    patched_gcode = patched_text.encode("utf-8")
    new_md5 = hashlib.md5(patched_gcode).hexdigest().upper()

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename == _GCODE_ENTRY:
                dst.writestr(info, patched_gcode)
            elif info.filename == _MD5_ENTRY:
                dst.writestr(info, new_md5)
            else:
                dst.writestr(info, src.read(info.filename))

    logger.info(
        "vol_speed patcher: ramped %d layers (flow_ratio=%.3f start_lin=%.2f step_lin=%.4f mm/s, mm3/mm=%.5f)",
        layers,
        effective_flow,
        start_lin,
        step_lin,
        mm3_per_mm,
    )
    return out.getvalue()
