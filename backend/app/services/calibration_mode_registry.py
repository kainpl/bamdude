"""Per-mode lifecycle registry for the Filament Calibration wizard (W2 Phase 0+).

Every calibration mode has one of three states. Source of truth for what's
implemented, what's safe to run on a real printer, and what surfaces as a
verification-only download for operator sign-off.

States
------
- ``DISABLED``: the wizard knows the mode exists but the slicing pipeline
  for it isn't ready. Frontend grays the row out; the server rejects
  ``start_calibration`` with ``409 mode_not_implemented``.
- ``VERIFICATION``: slicing pipeline is wired but not yet trusted to drive
  a real print. Frontend marks the row with a yellow pill and offers a
  single "Download sliced 3MF" button; the server runs the slice through
  the sidecar and returns the bytes as an HTTP attachment so the operator
  can compare against BS-produced fixtures locally. No queue, no
  ``filament_calibration`` row, no MQTT dispatch.
- ``PRODUCTION``: standard wizard flow — slice and enqueue via
  ``background_dispatch.enqueue_calibration_print``.

Lifecycle
---------
Each phase in ``temp/w2-calibration-implementation-plan.md`` §4 flips its
mode ``DISABLED`` → ``VERIFICATION`` at phase start; once the §5 sign-off
matrix row is green the same phase commit flips it ``VERIFICATION`` →
``PRODUCTION``. Code edits in *this* file are the only mechanism — no DB
toggles, no settings, no env vars. The ``VERIFICATION`` branch stays in
the codebase permanently as the initial state for any future mode.

The auto-paths (lidar-driven MQTT calibration) skip ``VERIFICATION``
entirely — there's nothing to download. They go ``DISABLED`` →
``PRODUCTION`` direct when their UI plumbing lands (Phase 8).
"""

from __future__ import annotations

from enum import Enum

from backend.app.services.calibration_constants import CaliMode


class ModeState(str, Enum):
    DISABLED = "disabled"
    VERIFICATION = "verification"
    PRODUCTION = "production"


# Source of truth for per-mode availability. Edit in the same commit that
# ships the per-mode implementation; never read from DB / settings. See
# module docstring for the lifecycle contract.
MODE_STATE: dict[CaliMode, ModeState] = {
    # Phase 1 — verification signed off 2026-05-14: side-by-side diff
    # against Orca-desktop's PA Tower wizard output matched at the
    # gcode-features level (identical inner_wall g1 count, identical
    # M900 K-band cadence, identical hollow geometry); slice output
    # imports clean into BS GUI without warnings; K-factor change
    # observed live on the first physical print. Promoted to PRODUCTION
    # so the wizard's session-dispatch path (POST /calibration/sessions)
    # accepts PA Tower jobs end-to-end.
    CaliMode.PA_TOWER: ModeState.PRODUCTION,
    # Phase 2 — PRODUCTION (2026-05-14). Builder regenerates the
    # pattern's custom_gcode_per_layer.xml on every call via a Python
    # port of BS CalibPressureAdvancePattern::generate_custom_gcodes
    # (Calib.cpp:506-656) — operator's start/end/step drive the K
    # sweep instead of relying on the BS-shipped scaffold's pre-baked
    # 0.0..0.08 step 0.005. Per-mode process+filament+printer preset
    # overrides applied before sidecar slice via
    # ``calib_preset_overrides.apply_pa_pattern_*`` (mirrors what BS
    # Plater::_calib_pa_pattern does in-memory) — verified via the
    # sliced CONFIG_BLOCK that ``wall_loops=3``,
    # ``initial_layer_speed=30``, ``line_width=nozzle*1.125`` etc. land
    # in the final gcode. Cube repositioned to Orca's project layout
    # (translate (51.63, 83.5, 0.4), kept 0.278×0.278×0.047 scale →
    # 5×5×0.85mm) so its perimeters don't overprint pattern V walls.
    CaliMode.PA_PATTERN: ModeState.PRODUCTION,
    # Phase 3 — PRODUCTION (2026-05-19). The builder bakes the temperature
    # tower (a two-plane mesh cut to the operator's [start, end] slab — the
    # tower carries embossed band numbers so it can't be Z-scaled) + the
    # 4-level overrides; the per-layer M104 ramp is then *inserted* into
    # the sliced g-code by calib_speed_ramp_patcher.patch_temp_tower (the
    # sidecar can't apply it — Calib_Temp_Tower is a GUI-only Print flag,
    # never carried in the 3MF). Verification signed off: re-sliced output
    # (both BS + Orca sidecar backends) carried the M104 ramp
    # 250→245→240→235→230 °C bit-identical to the reference at every band
    # boundary across all 250 layers; supports force-disabled. BOTH slice
    # paths patch — /slice-only and the start_calibration dispatch path.
    CaliMode.TEMP_TOWER: ModeState.PRODUCTION,
    # Phase 4 — PRODUCTION (2026-05-19). The builder mesh-cuts the
    # two-pillar tower (per body, capped) + bakes the overrides; the
    # per-layer retraction-length ramp (engine-side
    # ``Calib_Retraction_tower`` mutates the GCodeWriter's
    # ``retraction_length``, never carried in the 3MF) is re-created
    # post-slice by calib_speed_ramp_patcher (patch_retraction_tower) —
    # it scales every retraction move so each layer's total retraction
    # becomes ``start + floor(max(0, print_z - 0.4)) * step`` mm.
    # Verification signed off: re-sliced output matched the Orca-desktop
    # reference (engine ``; Calib_Retraction_tower`` comments) with 0
    # mismatches on all 107 layers, both Orca and BS sidecar backends —
    # including the uneven float-accumulated band boundaries. BOTH slice
    # paths patch — /slice-only and the start_calibration dispatch path.
    CaliMode.RETRACTION_TOWER: ModeState.PRODUCTION,
    # Phase 5 — PRODUCTION (2026-05-18). The builder bakes the tower
    # geometry + 4-level overrides; the per-layer outer-wall speed ramp is
    # rewritten into the sliced g-code by calib_speed_ramp_patcher
    # (patch_vfa_ramp) — the sidecar can't apply it (Calib_VFA_Tower is a
    # GUI-only Print flag, never carried in the 3MF). VFA shares the
    # Vol Speed patcher mechanism, banded per 5 mm; it uses the *precise*
    # patcher (running float-sum of the nominal layer height) so the
    # floor-banded ramp bit-matches the engine at every 5 mm boundary.
    # Verification signed off: re-sliced output matched the Orca-desktop
    # reference with 0 feedrate mismatches on all 425 layers, both Orca
    # and BS sidecar backends. BOTH slice paths patch — /slice-only and
    # the start_calibration dispatch path (calibration_service.py).
    CaliMode.VFA_TOWER: ModeState.PRODUCTION,
    # Phase 6 — PRODUCTION (2026-05-17). The builder bakes the tower
    # geometry + 4-level overrides; the per-layer outer-wall speed ramp
    # is rewritten into the sliced g-code by calib_speed_ramp_patcher (the
    # sidecar can't apply it — Calib_Vol_speed_Tower is a GUI-only Print
    # flag, never carried in the 3MF; a vanilla CLI slice yields a flat
    # 200 mm/s tower). Verification signed off: re-sliced output (Orca +
    # BS backends) patched to ramp law 26.05+2.604·z mm/s vs Orca-desktop
    # reference 26.03+2.604·z — match within integer-mm/s rounding noise.
    # BOTH slice paths patch — /slice-only and the start_calibration
    # dispatch path (calibration_service.py) — so dispatched towers ramp.
    CaliMode.VOL_SPEED_TOWER: ModeState.PRODUCTION,
    CaliMode.FLOW_RATE: ModeState.DISABLED,
    CaliMode.AUTO_PA_LINE: ModeState.DISABLED,
    # Phase 9 — PRODUCTION (2026-05-15). Operator verified the slice in
    # OrcaSlicer: pattern centres on the printer's real bed bbox (model
    # → bbox fallback for cloud-preset deltas without ``printable_area``),
    # 3×3×0.2 mm cube placeholder anchors to the right edge of the
    # glyph tab at its bottom row (tracks the centred layout for every
    # bed size). PA Line was previously documented as "permanently
    # DISABLED, use PA Pattern instead" but the port turned out to fit
    # the existing primitives (draw_digit / draw_number / draw_line /
    # draw_box) without a sidecar fork.
    CaliMode.PA_LINE: ModeState.PRODUCTION,
}


def get_mode_state(cali_mode: CaliMode | str) -> ModeState:
    """Return the lifecycle state for a mode. Unknown modes are ``DISABLED``.

    Accepts both the enum and its string value so route handlers don't
    have to coerce before asking.
    """
    if isinstance(cali_mode, str):
        try:
            cali_mode = CaliMode(cali_mode)
        except ValueError:
            return ModeState.DISABLED
    return MODE_STATE.get(cali_mode, ModeState.DISABLED)


def mode_state_map() -> dict[str, str]:
    """Snapshot of ``MODE_STATE`` keyed by the enum's ``.value`` strings.

    Used by ``compute_calibration_supports`` to project per-mode state into
    the wizard's capabilities response. Returned as plain strings so the
    JSON contract stays stable even if the enum names change.
    """
    return {mode.value: state.value for mode, state in MODE_STATE.items()}
