"""Unit tests for the pure helpers in ``services/auto_queue_eligibility.py``.

Covers ``_get_missing_force_color_slots`` and ``_count_override_color_matches``.
The full async ``find_eligible_printer`` flow is tested separately in
integration tests where it can exercise the DB and printer_manager wiring
end-to-end.
"""

from types import SimpleNamespace
from unittest.mock import patch

from backend.app.services.auto_queue_eligibility import (
    _count_override_color_matches,
    _get_missing_force_color_slots,
)


def _mock_status(ams: list[dict] | None = None, vt_tray: list[dict] | None = None):
    return SimpleNamespace(raw_data={"ams": ams or [], "vt_tray": vt_tray or [], "ams_extruder_map": {}})


class TestGetMissingForceColorSlots:
    def test_exact_match_no_missing(self) -> None:
        status = _mock_status(ams=[{"id": 0, "tray": [{"tray_type": "PLA", "tray_color": "#FF0000"}]}])
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=status):
            assert _get_missing_force_color_slots(1, [{"type": "PLA", "color": "#FF0000"}]) == []

    def test_color_mismatch_returns_descriptive_string(self) -> None:
        status = _mock_status(ams=[{"id": 0, "tray": [{"tray_type": "PLA", "tray_color": "#0000FF"}]}])
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=status):
            missing = _get_missing_force_color_slots(1, [{"type": "PLA", "color": "#FF0000"}])
            assert len(missing) == 1
            assert "PLA" in missing[0]
            assert "FF0000" in missing[0] or "#FF0000" in missing[0]

    def test_color_name_preferred_in_label(self) -> None:
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=None):
            # No status → all marked missing; label should use color_name when present
            missing = _get_missing_force_color_slots(1, [{"type": "PLA", "color": "#FF0000", "color_name": "Red"}])
            assert "Red" in missing[0]


class TestCountOverrideColorMatches:
    def test_zero_when_no_match(self) -> None:
        status = _mock_status(ams=[{"id": 0, "tray": [{"tray_type": "PLA", "tray_color": "#FFF"}]}])
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=status):
            assert _count_override_color_matches(1, [{"type": "PLA", "color": "#000"}]) == 0

    def test_counts_each_match(self) -> None:
        status = _mock_status(
            ams=[
                {
                    "id": 0,
                    "tray": [
                        {"tray_type": "PLA", "tray_color": "#FFFFFF"},
                        {"tray_type": "PETG", "tray_color": "#FF0000"},
                    ],
                }
            ]
        )
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=status):
            overrides = [
                {"type": "PLA", "color": "#FFFFFF"},
                {"type": "PETG", "color": "#FF0000"},
                {"type": "TPU", "color": "#0000FF"},  # not loaded
            ]
            assert _count_override_color_matches(1, overrides) == 2

    def test_no_status_returns_zero(self) -> None:
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=None):
            assert _count_override_color_matches(1, [{"type": "PLA", "color": "#FFF"}]) == 0
