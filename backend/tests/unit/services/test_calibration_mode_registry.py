"""Tests for calibration_mode_registry — W2 per-mode lifecycle."""

from __future__ import annotations

from backend.app.services.calibration_constants import CaliMode
from backend.app.services.calibration_mode_registry import (
    MODE_STATE,
    ModeState,
    get_mode_state,
    mode_state_map,
)


def test_mode_state_values():
    """Enum values must be the exact strings the frontend + JSON contract reads."""
    assert ModeState.DISABLED.value == "disabled"
    assert ModeState.VERIFICATION.value == "verification"
    assert ModeState.PRODUCTION.value == "production"


def test_mode_state_covers_every_cali_mode():
    """Every CaliMode must have an explicit MODE_STATE entry — adding a new
    mode without registering its lifecycle state would silently fall through
    to DISABLED, which is correct but masks the omission. Make the test the
    source of "did you remember to register this mode" pressure."""
    expected = set(CaliMode)
    registered = set(MODE_STATE.keys())
    missing = expected - registered
    assert not missing, f"CaliMode(s) missing from MODE_STATE: {missing}"


# Phases advance this set as each phase commit flips its mode out of
# DISABLED. Update the right-hand value to the expected post-phase state.
# The test below pins what's allowed; modes drifting into the wrong
# state (or non-listed modes flipping silently) fail the run.
_EXPECTED_NON_DISABLED: dict[CaliMode, ModeState] = {
    # Phase 1 (PA Tower) — promoted to PRODUCTION 2026-05-14 after
    # verification sign-off (gcode-features parity vs Orca-desktop +
    # clean BS-GUI import + live K-factor change on physical print).
    CaliMode.PA_TOWER: ModeState.PRODUCTION,
    # Phase 2 (PA Pattern) — promoted to PRODUCTION 2026-05-14 after
    # verification sign-off (sliced CONFIG_BLOCK confirms wall_loops=3,
    # initial_layer_speed=30, line_width=nozzle*1.125 etc. land in the
    # final gcode via calib_preset_overrides.apply_pa_pattern_*; cube
    # repositioned to Orca's layout (51.63, 83.5, 0.4) so V walls have
    # clear bed).
    CaliMode.PA_PATTERN: ModeState.PRODUCTION,
}


def test_mode_states_match_expected_phase_progression():
    """Each phase commit edits ``_EXPECTED_NON_DISABLED`` alongside the
    ``MODE_STATE`` change. Drift between code + test is the loud signal
    that "did you remember the per-phase test update too?" — without
    that pressure, a flip can land silently and lose the wave."""
    actual_non_disabled = {m: s for m, s in MODE_STATE.items() if s != ModeState.DISABLED}
    assert actual_non_disabled == _EXPECTED_NON_DISABLED, (
        "MODE_STATE non-DISABLED entries drifted from the expected progression. "
        f"actual={actual_non_disabled}, expected={_EXPECTED_NON_DISABLED}. "
        "If you're adding a phase, update _EXPECTED_NON_DISABLED in this test."
    )


def test_get_mode_state_accepts_enum_and_string():
    assert get_mode_state(CaliMode.PA_TOWER) == ModeState.PRODUCTION
    assert get_mode_state("pa_tower") == ModeState.PRODUCTION


def test_get_mode_state_unknown_returns_disabled():
    """Unknown mode strings fall back to DISABLED — defensive guard for
    API callers asking about a mode that's been removed from the enum."""
    assert get_mode_state("not_a_mode") == ModeState.DISABLED


def test_mode_state_map_string_keys():
    """Projection for the capabilities API: keys are CaliMode.value strings,
    values are ModeState.value strings. Used by frontend mode_state."""
    snapshot = mode_state_map()
    assert snapshot["pa_tower"] == "production"
    assert all(isinstance(k, str) for k in snapshot)
    assert all(v in ("disabled", "verification", "production") for v in snapshot.values())
    # Same coverage as the enum test — every CaliMode lands in the projected map
    assert set(snapshot.keys()) == {m.value for m in CaliMode}
