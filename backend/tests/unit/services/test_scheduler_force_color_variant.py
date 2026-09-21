"""Force colour match tells Bambu's PLA sub-variants apart (upstream #2650).

Bambu reports Basic, Matte and Silk all as ``tray_type == "PLA"``; the variant
lives only in ``tray_info_idx`` (GFA00 / GFA01 / GFA06).
:func:`auto_queue_eligibility._get_missing_force_color_slots` decides *which
printer* an item may go to, and is pinned here.

⚠️ The other half — *which tray on that printer* — was a second matcher in
``auto_queue_ams``, and its half of this file went with it: binding a slot to a
feed is ``services/filament_routing``'s single job now, under the item's own
``RoutingPolicy``, and ``unit/services/test_filament_routing.py`` is where the
variant rule is pinned for it.
"""

from types import SimpleNamespace
from unittest.mock import patch

import backend.app.core.database  # noqa: F401 — registers every mapper
import backend.app.models.printer_location  # noqa: F401 — Printer.location resolves against it
from backend.app.services.auto_queue_eligibility import _get_missing_force_color_slots

BASIC = "GFA00"
MATTE = "GFA01"


def _mock_status(ams: list[dict] | None = None, vt_tray: list[dict] | None = None):
    return SimpleNamespace(
        raw_data={"ams": ams or [], "vt_tray": vt_tray or [], "ams_extruder_map": {}},
        ams_auto_switch_filament=None,
    )


def _white_pla(tray_id: int, idx: str) -> dict:
    return {"id": tray_id, "tray_type": "PLA", "tray_color": "#FFFFFF", "tray_info_idx": idx}


class TestEligibilityVariantMatching:
    """Which printer the item may be routed to."""

    def test_same_colour_different_variant_is_not_a_match(self) -> None:
        """A job sliced for PLA Matte must not accept a printer holding PLA Basic."""
        status = _mock_status(ams=[{"id": 0, "tray": [_white_pla(0, BASIC)]}])
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=status):
            missing = _get_missing_force_color_slots(
                1, [{"type": "PLA", "color": "#FFFFFF", "tray_info_idx": MATTE, "color_name": "White"}]
            )
        assert missing == ["PLA (White)"]

    def test_matching_variant_is_a_match(self) -> None:
        status = _mock_status(ams=[{"id": 0, "tray": [_white_pla(0, MATTE)]}])
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=status):
            assert (
                _get_missing_force_color_slots(1, [{"type": "PLA", "color": "#FFFFFF", "tray_info_idx": MATTE}]) == []
            )

    def test_the_right_variant_beside_a_wrong_one_still_matches(self) -> None:
        """Two same-colour spools, one of each variant — the set-of-pairs shape
        this replaced could not see past the first."""
        status = _mock_status(ams=[{"id": 0, "tray": [_white_pla(0, BASIC), _white_pla(1, MATTE)]}])
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=status):
            assert (
                _get_missing_force_color_slots(1, [{"type": "PLA", "color": "#FFFFFF", "tray_info_idx": MATTE}]) == []
            )

    def test_blank_idx_on_the_tray_falls_back_to_type_and_colour(self) -> None:
        """Third-party spools report no idx — they must still satisfy the demand."""
        status = _mock_status(ams=[{"id": 0, "tray": [_white_pla(0, "")]}])
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=status):
            assert (
                _get_missing_force_color_slots(1, [{"type": "PLA", "color": "#FFFFFF", "tray_info_idx": MATTE}]) == []
            )

    def test_blank_idx_on_the_override_falls_back_to_type_and_colour(self) -> None:
        """A 3MF sliced before the field existed carries none — unchanged behaviour."""
        status = _mock_status(ams=[{"id": 0, "tray": [_white_pla(0, BASIC)]}])
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=status):
            assert _get_missing_force_color_slots(1, [{"type": "PLA", "color": "#FFFFFF"}]) == []

    def test_external_spool_carries_its_variant_too(self) -> None:
        status = _mock_status(vt_tray=[{"tray_type": "PLA", "tray_color": "#FFFFFF", "tray_info_idx": BASIC}])
        with patch("backend.app.services.auto_queue_eligibility.printer_manager.get_status", return_value=status):
            missing = _get_missing_force_color_slots(1, [{"type": "PLA", "color": "#FFFFFF", "tray_info_idx": MATTE}])
        assert missing == ["PLA (#FFFFFF)"]
