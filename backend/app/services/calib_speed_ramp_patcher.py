"""Post-slice per-layer g-code patchers for the calibration towers —
Volumetric Speed (W2 Phase 6), VFA (W2 Phase 5), Temp (W2 Phase 3) and
Retraction (W2 Phase 4).

The speed towers ramp the outer-wall *feedrate*; the Temp tower ramps the
nozzle *temperature*; the Retraction tower ramps the *retraction length*.
BS/Orca produce these engine-side (the ``Calib_Vol_speed_Tower`` /
``Calib_VFA_Tower`` / ``Calib_Temp_Tower`` / ``Calib_Retraction_tower``
cases in ``GCode.cpp``), but those branches fire only when
``Print::calib_mode()`` is set — a GUI-only in-memory flag that is **not
carried in the 3MF**. A vanilla CLI / sidecar slice therefore produces a
flat tower; BamDude re-creates the ramp post-slice.

Three shapes of per-layer edit:

- **Rewrite** — Vol Speed / VFA mutate ``m_calib_config`` and emit no
  g-code line, so the slicer's existing bare ``G1 F`` per layer is
  *rewritten* (:func:`patch_vol_speed_ramp`, :func:`patch_vfa_ramp`).
- **Insert** — Temp's engine case actually appends an ``M104`` line, so
  the slicer (with ``calib_mode`` unset) emits *no* per-layer temperature
  command; the patcher *inserts* one per layer (:func:`patch_temp_tower`).
- **Scale** — Retraction's engine case mutates the GCodeWriter's
  ``retraction_length`` — the value behind *every* ``G1 E`` retraction
  move, not a single line. The patcher *scales* every retraction move in
  a layer by ``length(z) / preset_length`` (:func:`patch_retraction_tower`).

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
from collections import Counter
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


def _apply_gcode_patch(
    threemf_bytes: bytes,
    gcode_patch: Callable[[str], tuple[str, int]],
    label: str,
) -> bytes:
    """Run ``gcode_patch`` over the 3MF's plate g-code and repack.

    ``gcode_patch`` takes the g-code text and returns
    ``(patched_text, layers_touched)``. Returns the repacked 3MF bytes
    (g-code entry rewritten, ``.gcode.md5`` sidecar recomputed); all other
    entries are copied verbatim.
    """
    src = zipfile.ZipFile(io.BytesIO(threemf_bytes))
    if _GCODE_ENTRY not in src.namelist():
        raise ValueError(f"{label} patcher: 3MF has no {_GCODE_ENTRY} — was export_3mf set?")

    gcode_text = src.read(_GCODE_ENTRY).decode("utf-8", "replace")
    patched_text, layers = gcode_patch(gcode_text)
    if layers == 0:
        raise ValueError(
            f"{label} patcher: no layers patched — sliced g-code structure "
            "not recognised (expected '; CHANGE_LAYER' layer blocks)."
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
    return _apply_gcode_patch(threemf_bytes, lambda t: _patch_gcode(t, speed_fn), label)


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
    return _apply_gcode_patch(threemf_bytes, lambda t: _patch_gcode_precise(t, speed_fn), label)


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


def _patch_gcode_insert_temp(text: str, temp_fn: Callable[[float], float]) -> tuple[str, int]:
    """Insert one ``M104`` per layer via ``temp_fn(print_z) -> °C``.

    Unlike the speed patchers this does not rewrite an existing line — the
    sliced g-code (``calib_mode`` unset) carries no per-layer temperature
    command — so an ``M104 S<temp>`` line is *inserted* right after each
    layer's ``; Z_HEIGHT`` comment. Returns ``(patched_text, layers)``.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    pending = False
    inserted = 0
    for line in lines:
        out.append(line)
        if line.startswith("; CHANGE_LAYER"):
            pending = True
            continue
        if pending:
            mz = _Z_HEIGHT.match(line)
            if mz:
                temp = round(temp_fn(float(mz.group(1))))
                eol = line[len(line.rstrip("\r\n")) :] or "\n"
                out.append(f"M104 S{temp} ; calib temp{eol}")
                inserted += 1
                pending = False
    return "".join(out), inserted


def patch_temp_tower(threemf_bytes: bytes, *, start: float) -> bytes:
    """Insert the per-layer nozzle-temperature ramp into a sliced Temp 3MF.

    Mirrors the ``Calib_Temp_Tower`` g-code case:
    ``temp(z) = start - floor(print_z / 10.001)·5`` °C — banded, 10 mm per
    band, 5 °C step, descending. The ``10.001`` divisor (verbatim from BS)
    shifts every band boundary off the layer grid, so the rounded
    ``; Z_HEIGHT`` comment is exact enough — no precise variant needed.
    """
    return _apply_gcode_patch(
        threemf_bytes,
        lambda t: _patch_gcode_insert_temp(t, lambda z: start - math.floor(z / 10.001) * 5.0),
        label="temp",
    )


# -- Retraction tower -----------------------------------------------------

# A retraction move: a G0/G1 carrying an E word. The E-only deretract and
# the F feedrate never trip the axis test (F is not X/Y/Z).
_G_MOVE = re.compile(r"^G[01](?= |$)")
_E_WORD = re.compile(r"(?<= )E(-?[0-9]*\.?[0-9]+)")
_AXIS_WORD = re.compile(r"(?<= )[XYZ]-?[0-9.]")
# End of the printable body — past here is filament / machine end g-code,
# whose retraction moves (e.g. ``G1 E-0.8 ; retract``) must NOT be scaled.
_END_GCODE_MARKERS = ("; filament end gcode", "; MACHINE_END_GCODE_START", "; EXECUTABLE_BLOCK_END")


def _fmt_e(value: float) -> str:
    """Format a scaled E value the way the slicer writes them — up to 5
    decimal places, trailing zeros stripped, a bare ``0`` when zeroed."""
    s = f"{value:.5f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("", "-", "-0") else s


def _scale_retraction_line(line: str, factor: float) -> str | None:
    """Scale the E word of a retraction / deretraction move by ``factor``.

    Returns the rewritten line, or ``None`` when the line is not a
    retraction move. In Bambu's relative-E (``M83``) g-code:

    - **negative E** — a retract, pure (``G1 E-x``) or wipe-while-retract
      (``G1 X.. Y.. E-x``); both are scaled.
    - **E-only positive** — a deretract (``G1 E+x``); scaled.
    - **positive E with an X/Y/Z word** — real wall extrusion; left alone.
    """
    stripped = line.rstrip("\r\n")
    eol = line[len(stripped) :]
    code, sep, comment = stripped.partition(";")
    code_r = code.rstrip()
    if not _G_MOVE.match(code_r):
        return None
    m = _E_WORD.search(code_r)
    if not m:
        return None
    e = float(m.group(1))
    if e == 0.0:
        return None
    if e > 0 and _AXIS_WORD.search(code_r):
        return None  # real extrusion move — never touched
    new_code = code[: m.start(1)] + _fmt_e(e * factor) + code[m.end(1) :]
    return new_code + sep + comment + eol


def _measure_retraction_length(lines: list[str], start_idx: int, end_idx: int) -> float | None:
    """The slice's one constant preset retraction length, measured from
    the g-code: the most common E-only positive (deretract) move in the
    printable body. Measured rather than read from the config so it is
    immune to printer/filament preset-precedence (a filament
    ``filament_retraction_length`` overrides the printer value)."""
    vals: list[float] = []
    for i in range(start_idx, end_idx):
        code = lines[i].split(";", 1)[0].rstrip()
        if not _G_MOVE.match(code):
            continue
        m = _E_WORD.search(code)
        if not m:
            continue
        e = float(m.group(1))
        if e > 0 and not _AXIS_WORD.search(code):
            vals.append(round(e, 5))
    if not vals:
        return None
    return Counter(vals).most_common(1)[0][0]


def _patch_gcode_retraction(text: str, start: float, step: float) -> tuple[str, int]:
    """Scale every retraction move so each layer's retraction length is
    ``start + floor(max(0, print_z - 0.4)) * step`` mm.

    Verbatim from the ``Calib_Retraction_tower`` g-code case — banded,
    1 mm per band. ``print_z`` is the **precise** running float-sum of the
    per-layer layer height (the band boundary z = 1.4, 2.4, … lands on the
    0.2 mm layer grid, so the rounded ``; Z_HEIGHT`` comment would flip a
    band). Every retraction move in a layer is scaled by
    ``length(z) / preset_length`` — structure-agnostic, so it handles both
    BS's and Orca's differing wipe-while-retract splits, and keeps each
    retract/deretract balanced because the factor is constant per layer.
    """
    lines = text.splitlines(keepends=True)
    n = len(lines)

    first = next((i for i, ln in enumerate(lines) if ln.startswith("; CHANGE_LAYER")), None)
    if first is None:
        return text, 0
    body_end = n
    for i in range(first, n):
        s = lines[i].lstrip()
        if any(s.startswith(mk) for mk in _END_GCODE_MARKERS):
            body_end = i
            break

    retract_len = _measure_retraction_length(lines, first, body_end)
    if not retract_len or retract_len <= 0:
        return text, 0

    accum_z = 0.0
    factor = 0.0
    patched = 0
    for i in range(first, body_end):
        line = lines[i]
        if line.startswith("; CHANGE_LAYER"):
            continue
        ml = _LAYER_HEIGHT.match(line)
        if ml:
            # Running float-sum of the nominal layer height — bit-matches
            # the engine's print_z accumulation (see _patch_gcode_precise).
            accum_z += round(float(ml.group(1)), 3)
            length = start + math.floor(max(0.0, accum_z - 0.4)) * step
            factor = length / retract_len
            continue
        new = _scale_retraction_line(line, factor)
        if new is not None:
            lines[i] = new
            patched += 1
    return "".join(lines), patched


def patch_retraction_tower(threemf_bytes: bytes, *, start: float, step: float) -> bytes:
    """Rewrite a sliced Retraction-tower 3MF so each layer's retraction
    length follows the BS/Orca ``Calib_Retraction_tower`` ramp.

    Unlike the speed towers (rewrite one ``G1 F``) and Temp (insert one
    ``M104``), the retraction engine case mutates the GCodeWriter's
    ``retraction_length`` — the value behind *every* ``G1 E`` retraction
    move. A vanilla slice holds them all at the preset's one constant
    length; this patcher scales every retraction (pure retract + wipe-
    while-retract) and every deretraction so each layer's total retraction
    becomes ``start + floor(max(0, print_z - 0.4)) * step`` mm.

    ``start`` / ``step`` are the operator's retraction sweep in mm.
    """
    return _apply_gcode_patch(
        threemf_bytes,
        lambda t: _patch_gcode_retraction(t, start, step),
        label="retraction",
    )
