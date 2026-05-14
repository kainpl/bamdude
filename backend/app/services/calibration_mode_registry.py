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
    CaliMode.PA_PATTERN: ModeState.DISABLED,
    CaliMode.TEMP_TOWER: ModeState.DISABLED,
    CaliMode.RETRACTION_TOWER: ModeState.DISABLED,
    CaliMode.VFA_TOWER: ModeState.DISABLED,
    CaliMode.VOL_SPEED_TOWER: ModeState.DISABLED,
    CaliMode.FLOW_RATE: ModeState.DISABLED,
    CaliMode.AUTO_PA_LINE: ModeState.DISABLED,
    # PA_LINE is documented as permanently DISABLED — BS engine-side path
    # generation (GCode.cpp:2514/2658/2817/2843) can't be expressed through
    # custom_gcode_per_layer.xml. Operators are pointed at PA Pattern.
    CaliMode.PA_LINE: ModeState.DISABLED,
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
