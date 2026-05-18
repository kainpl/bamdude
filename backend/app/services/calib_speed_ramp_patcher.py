"""Post-slice per-layer feedrate-ramp patchers for the speed calibration
towers — Volumetric Speed (W2 Phase 6) and VFA (W2 Phase 5).

Both modes ramp the outer-wall speed up the tower. BS/Orca produce that
ramp engine-side (the ``Calib_Vol_speed_Tower`` / ``Calib_VFA_Tower``
cases in ``GCode.cpp`` mutate ``m_calib_config["outer_wall_speed"]`` per
layer), but those branches fire only when ``Print::calib_mode()`` is set
— a GUI-only in-memory flag that is **not carried in the 3MF**. A vanilla
CLI / sidecar slice therefore produces a flat-speed tower, useless for
calibration (verified for Vol Speed against two sidecar backends).

BamDude re-creates the ramp by rewriting the per-layer feedrate in the
sliced g-code. In spiral/vase mode the whole layer is one outer-wall
spiral, so each layer carries exactly one bare ``G1 F<feedrate>`` line —
that is the line :func:`patch_layer_feedrate` rewrites (``F`` is mm/min,
hence the ``·60``). The first layer keeps its slicer-assigned first-layer
speed; the ramp covers layers 1..N.

Per-mode speed functions (verbatim from BS/Orca ``GCode.cpp``):

- Vol Speed: ``speed(z) = start_lin + z·step_lin`` — continuous; the
  operator's params are *volumetric* (mm³/s) and are divided by
  ``mm3_per_mm`` to get the linear (mm/s) ramp.
- VFA: ``speed(z) = start + floor(z/5)·step`` — banded in 5 mm steps;
  the operator's params are *linear* (mm/s) already, no transform.

Two patcher variants share the g-code rewrite:

- :func:`patch_layer_feedrate` (**regular**) reads the rounded
  ``; Z_HEIGHT`` comment. Fine for a *continuous* ramp (Vol Speed) where
  a ~1e-6 rounding gap can't change the rounded result.
- :func:`patch_layer_feedrate_precise` (**precise**) reconstructs
  ``print_z`` by a running float-sum of ``; LAYER_HEIGHT`` comments,
  bit-matching the engine's own accumulation. Required for a *banded*
  (``floor``-ed) ramp like VFA, whose band boundary the rounded
  ``Z_HEIGHT`` would shift by a layer. New modes: try regular first,
  switch to precise only if a verification slice-diff shows a mismatch.

NOTE for the PRODUCTION promotion of either mode: the production
dispatch path slices independently of ``/slice-only`` — it must call the
matching patcher on its sliced 3MF too, or dispatched towers print flat.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import re
import zipfile
from collections.abc import Callable

logger = logging.getLogger(__name__)

_GCODE_ENTRY = "Metadata/plate_1.gcode"
_MD5_ENTRY = "Metadata/plate_1.gcode.md5"
_PROJECT_CONFIG_ENTRY = "Metadata/project_settings.config"

# A layer's print feedrate: a G1 carrying only an F word (no X/Y/Z/E).
# Travel lifts emit "G1 Z.. F.." (has Z) so they never match.
_BARE_F = re.compile(r"^G1 F([\d.]+)\s*$")
_Z_HEIGHT = re.compile(r";\s*Z_HEIGHT:\s*([\d.]+)")
_LAYER_HEIGHT = re.compile(r";\s*LAYER_HEIGHT:\s*([\d.]+)")


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


def _patch_gcode(text: str, speed_fn: Callable[[float], float]) -> tuple[str, int]:
    """Rewrite the per-layer feedrate via ``speed_fn(print_z) -> mm/s``.

    Returns ``(patched_text, layers_patched)``.
    """
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
                        speed = round(speed_fn(z_h))
                        feedrate = max(1, speed) * 60
                        eol = lines[j][len(lines[j].rstrip("\r\n")) :]
                        lines[j] = f"G1 F{feedrate}{eol}"
                        patched += 1
                j += 1
            idx = j
        else:
            idx += 1
    return "".join(lines), patched


def _patch_gcode_precise(text: str, speed_fn: Callable[[float], float]) -> tuple[str, int]:
    """Like :func:`_patch_gcode`, but reconstructs each layer's ``print_z``
    by a running float-sum of the per-layer layer height instead of
    reading the rounded ``; Z_HEIGHT`` comment.

    The slicer engine accumulates ``print_z`` as a running sum of the
    config ``layer_height``. IEEE 754 is deterministic, so repeating the
    same additions yields the *same* (imprecise) ``print_z`` the engine
    used for its band maths — bit-for-bit. The ``Z_HEIGHT`` comment is
    rounded for display (``10.0`` where the engine held
    ``9.999999999999996``), which flips ``floor()``-banded ramps (VFA) by
    one layer at every 5 mm boundary. See
    ``temp/vfa-calibration-bs-orca-analysis.md`` §8.

    NB the ``; LAYER_HEIGHT`` comment is *not* the accumulation input —
    it is the float *delta* of two accumulated ``print_z`` values, so it
    drifts (``0.200001``, ``0.199997``, …). Rounding it to 3 dp recovers
    the nominal config layer height, which is what the engine actually
    adds each layer.
    """
    lines = text.splitlines(keepends=True)
    n = len(lines)
    idx = 0
    block_index = -1
    patched = 0
    accum_z = 0.0
    while idx < n:
        if lines[idx].startswith("; CHANGE_LAYER"):
            block_index += 1
            z_h: float | None = None
            j = idx + 1
            while j < n and not lines[j].startswith("; CHANGE_LAYER"):
                ml = _LAYER_HEIGHT.match(lines[j])
                if ml:
                    # Running sum of the nominal layer height — must match
                    # the engine's accumulation order, so add exactly once
                    # per layer block.
                    accum_z += round(float(ml.group(1)), 3)
                    z_h = accum_z
                if block_index >= 1 and z_h is not None:
                    mf = _BARE_F.match(lines[j].rstrip("\r\n"))
                    if mf:
                        speed = round(speed_fn(z_h))
                        feedrate = max(1, speed) * 60
                        eol = lines[j][len(lines[j].rstrip("\r\n")) :]
                        lines[j] = f"G1 F{feedrate}{eol}"
                        patched += 1
                j += 1
            idx = j
        else:
            idx += 1
    return "".join(lines), patched


def _apply_feedrate_patch(
    threemf_bytes: bytes,
    gcode_patch: Callable[[str], tuple[str, int]],
    label: str,
) -> bytes:
    """Run ``gcode_patch`` over the 3MF's plate g-code and repack.

    Returns the repacked 3MF bytes (g-code entry rewritten, ``.gcode.md5``
    sidecar recomputed); all other entries are copied verbatim.
    """
    src = zipfile.ZipFile(io.BytesIO(threemf_bytes))
    if _GCODE_ENTRY not in src.namelist():
        raise ValueError(f"{label} patcher: 3MF has no {_GCODE_ENTRY} — was export_3mf set?")

    gcode_text = src.read(_GCODE_ENTRY).decode("utf-8", "replace")
    patched_text, layers = gcode_patch(gcode_text)
    if layers == 0:
        raise ValueError(
            f"{label} patcher: no layer feedrate lines rewritten — sliced g-code "
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

    logger.info("%s patcher: ramped %d layers", label, layers)
    return out.getvalue()


def patch_layer_feedrate(
    threemf_bytes: bytes,
    speed_fn: Callable[[float], float],
    *,
    label: str,
) -> bytes:
    """Rewrite a sliced 3MF so each layer's outer-wall feedrate follows ``speed_fn``.

    **Regular** patcher — reads the rounded ``; Z_HEIGHT`` comment for each
    layer's height. ``speed_fn(print_z)`` returns the target speed in mm/s;
    it is rounded and converted to a mm/min ``G1 F`` feedrate. ``label``
    names the calling mode for logs / error messages.

    Used by every mode whose ramp is *continuous* in z (Vol Speed) — there
    the rounded ``Z_HEIGHT`` is exact enough. A *banded* ramp (``floor``-ed,
    like VFA) needs :func:`patch_layer_feedrate_precise` instead, which
    matches the engine's float-accumulated ``print_z`` at band boundaries.
    For a new mode, try this first; switch to the precise variant only if a
    verification slice-diff shows a boundary mismatch.
    """
    return _apply_feedrate_patch(threemf_bytes, lambda t: _patch_gcode(t, speed_fn), label)


def patch_layer_feedrate_precise(
    threemf_bytes: bytes,
    speed_fn: Callable[[float], float],
    *,
    label: str,
) -> bytes:
    """Like :func:`patch_layer_feedrate`, but reconstructs each layer's
    ``print_z`` by a running float-sum of the ``; LAYER_HEIGHT`` comments
    so it bit-matches the slicer engine's own ``print_z`` — see
    :func:`_patch_gcode_precise`. Needed for ``floor``-banded ramps whose
    band boundary the rounded ``Z_HEIGHT`` comment would otherwise shift.
    """
    return _apply_feedrate_patch(threemf_bytes, lambda t: _patch_gcode_precise(t, speed_fn), label)


def patch_vol_speed_ramp(
    threemf_bytes: bytes,
    *,
    start: float,
    step: float,
    nozzle_diameter: float,
    flow_ratio: float | None = None,
) -> bytes:
    """Rewrite a sliced Vol-Speed 3MF so each layer ramps the outer-wall feedrate.

    ``start``/``step`` are the volumetric (mm³/s) sweep params. ``flow_ratio``
    is read from the sliced 3MF's resolved ``project_settings.config`` when
    left ``None`` (authoritative — see :func:`_resolve_flow_ratio`); pass an
    explicit value only to override. Mirrors BS/Orca
    ``Plater::calib_max_vol_speed`` + the ``Calib_Vol_speed_Tower`` g-code
    case: ``speed(z) = start/mm3_per_mm + z·step/mm3_per_mm``.
    """
    src = zipfile.ZipFile(io.BytesIO(threemf_bytes))
    effective_flow = _resolve_flow_ratio(src, flow_ratio if flow_ratio is not None else 1.0)
    line_width = nozzle_diameter * 1.75
    layer_height = nozzle_diameter * 0.8
    mm3_per_mm = _flow_mm3_per_mm(line_width, layer_height, effective_flow)
    if mm3_per_mm <= 0:
        raise ValueError("vol_speed patcher: non-positive mm3_per_mm — bad nozzle/flow params")
    start_lin = start / mm3_per_mm
    step_lin = step / mm3_per_mm

    logger.info(
        "vol_speed patcher: flow_ratio=%.3f start_lin=%.2f step_lin=%.4f mm/s (mm3/mm=%.5f)",
        effective_flow,
        start_lin,
        step_lin,
        mm3_per_mm,
    )
    return patch_layer_feedrate(
        threemf_bytes,
        lambda z: start_lin + z * step_lin,
        label="vol_speed",
    )


def patch_vfa_ramp(threemf_bytes: bytes, *, start: float, step: float) -> bytes:
    """Rewrite a sliced VFA 3MF so each layer ramps the outer-wall feedrate.

    ``start``/``step`` are the *linear* (mm/s) sweep params — VFA needs no
    volumetric transform. Mirrors the ``Calib_VFA_Tower`` g-code case:
    ``speed(z) = start + floor(z / 5)·step`` — the speed is held constant
    within each 5 mm band. Uses the **precise** patcher: the ``floor``
    banding flips by one layer at every 5 mm boundary unless ``print_z``
    bit-matches the engine's float-accumulated value.
    """
    return patch_layer_feedrate_precise(
        threemf_bytes,
        lambda z: start + math.floor(z / 5.0) * step,
        label="vfa",
    )
