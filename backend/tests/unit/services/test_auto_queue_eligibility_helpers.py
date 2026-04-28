"""Unit tests for the pure helpers in ``services/auto_queue_eligibility.py``.

Covers ``_get_missing_filament_types``, ``_get_missing_force_color_slots``,
and ``_count_override_color_matches``. The full async ``find_eligible_printer``
flow is tested separately in integration tests where it can exercise
the DB and printer_manager wiring end-to-end.
"""

from types import SimpleNamespace
from unittest.mock import patch

from backend.app.services.auto_queue_eligibility import (
    _count_override_color_matches,
    _get_missing_filament_types,
    _get_missing_force_color_slots,
)


def _mock_status(ams: list[dict] | None = None, vt_tray: list[dict] | None = None):
    return SimpleNamespace(raw_data={"ams": ams or [], "vt_tray": vt_tray or [], "ams_extruder_map": {}})


class TestGetMissingFilamentTypes:
    def test_all_loaded_returns_empty(self) -> None:
        status = _mock_status(ams=[{"id": 0, "tray": [{"tray_type": "PLA"}, {"tray_type": "PETG"}]}])
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=status):
            assert _get_missing_filament_types(1, ["PLA", "PETG"]) == []

    def test_one_missing_listed(self) -> None:
        status = _mock_status(ams=[{"id": 0, "tray": [{"tray_type": "PLA"}]}])
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=status):
            assert _get_missing_filament_types(1, ["PLA", "PETG"]) == ["PETG"]

    def test_canonical_equivalence_pa_cf(self) -> None:
        """PA-CF / PA12-CF / PAHT-CF are equivalent for matching."""
        status = _mock_status(ams=[{"id": 0, "tray": [{"tray_type": "PA12-CF"}]}])
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=status):
            # Required is PA-CF, loaded is PA12-CF — should match via canonical map
            assert _get_missing_filament_types(1, ["PA-CF"]) == []

    def test_external_vt_tray_counted(self) -> None:
        status = _mock_status(vt_tray=[{"tray_type": "TPU"}])
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=status):
            assert _get_missing_filament_types(1, ["TPU"]) == []

    def test_no_status_returns_all_required(self) -> None:
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=None):
            assert _get_missing_filament_types(1, ["PLA", "PETG"]) == ["PLA", "PETG"]


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
