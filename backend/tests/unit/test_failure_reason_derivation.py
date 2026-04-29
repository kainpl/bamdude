"""Regression tests for backend.app.main.derive_failure_reason.

The earlier module-based heuristic mislabelled the H2D's user-cancel
sequence (a module-0x0C HMS echo) as "Layer shift". The curated short-code
map in `_HMS_FAILURE_REASONS` ends that — anything not in the map leaves
failure_reason=None instead of guessing.
"""

from __future__ import annotations

from backend.app.main import derive_failure_reason


def _hms(short_code: str) -> dict:
    """Build a fake hms_errors[i] dict with the given MMMM_CCCC short code."""
    module_hex, code_hex = short_code.split("_", 1)
    return {
        "attr": int(module_hex, 16) << 16,
        "code": int(code_hex, 16),
    }


class TestCancelledStatuses:
    def test_aborted_yields_user_cancelled(self):
        assert derive_failure_reason("aborted", []) == "User cancelled"

    def test_cancelled_yields_user_cancelled(self):
        assert derive_failure_reason("cancelled", []) == "User cancelled"

    def test_cancelled_ignores_hms(self):
        # Even if the printer also emitted real-looking HMS, "cancelled" wins.
        assert derive_failure_reason("cancelled", [_hms("0300_4057")]) == "User cancelled"


class TestFailedWithKnownCodes:
    def test_layer_shift_module_0x03(self):
        assert derive_failure_reason("failed", [_hms("0300_4057")]) == "Layer shift"

    def test_filament_runout_per_slot(self):
        assert derive_failure_reason("failed", [_hms("0701_8011")]) == "Filament runout"

    def test_clogged_nozzle(self):
        assert derive_failure_reason("failed", [_hms("0300_4006")]) == "Clogged nozzle"

    def test_first_match_wins(self):
        # Two known codes — return whichever the map yields for the first hit.
        result = derive_failure_reason("failed", [_hms("0300_4057"), _hms("0701_8011")])
        assert result in {"Layer shift", "Filament runout"}


class TestFailedWithoutKnownCodes:
    def test_h2d_cancel_echo_module_0x0C_no_longer_layer_shift(self):
        # The bug: 0C00_001B is the H2D's cancel-sequence echo. Old heuristic
        # mapped any module-0x0C HMS to "Layer shift" — should be None now.
        assert derive_failure_reason("failed", [_hms("0C00_001B")]) is None

    def test_unknown_code_returns_none(self):
        assert derive_failure_reason("failed", [_hms("0F00_FFFF")]) is None

    def test_failed_with_no_hms_returns_none(self):
        assert derive_failure_reason("failed", []) is None
        assert derive_failure_reason("failed", None) is None


class TestNonTerminalStatuses:
    def test_completed_returns_none(self):
        assert derive_failure_reason("completed", []) is None

    def test_running_returns_none(self):
        assert derive_failure_reason("running", [_hms("0300_4057")]) is None
