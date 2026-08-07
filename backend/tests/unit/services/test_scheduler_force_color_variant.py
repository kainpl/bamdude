"""Force colour match tells Bambu's PLA sub-variants apart (upstream #2650).

Bambu reports Basic, Matte and Silk all as ``tray_type == "PLA"``; the variant
lives only in ``tray_info_idx`` (GFA00 / GFA01 / GFA06). Two halves have to
agree or the farm gets worse, not better:

* :func:`auto_queue_eligibility._get_missing_force_color_slots` decides *which
  printer* an item may go to, and
* :func:`auto_queue_ams.compute_ams_mapping_for_printer` decides *which tray on
  that printer* the slot maps to.

A matcher that distinguishes variants while the slot mapper does not would
dispatch onto the very tray the matcher had just rejected — so both are pinned
here, together.

The last class covers a crash found while porting this: the per-printer
scheduler still carried an override block for a column ``PrintQueueItem`` has
not had since m002.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import backend.app.core.database  # noqa: F401 — registers every mapper
import backend.app.models.printer_location  # noqa: F401 — Printer.location resolves against it
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.auto_queue_ams import compute_ams_mapping_for_printer
from backend.app.services.auto_queue_eligibility import _get_missing_force_color_slots
from backend.app.services.print_scheduler import scheduler

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


@pytest.mark.asyncio
class TestSlotMappingPinsTheVariant:
    """Which tray on that printer the slot maps to."""

    @staticmethod
    def _two_white_spools():
        # Tray 0 = Basic (global id 0), tray 1 = Matte (global id 1).
        return _mock_status(ams=[{"id": 0, "tray": [_white_pla(0, BASIC), _white_pla(1, MATTE)]}])

    @staticmethod
    def _item(overrides: list[dict]):
        return SimpleNamespace(id=1, filament_overrides=json.dumps(overrides), plate_id=None)

    async def _map(self, item, requirements):
        with (
            patch(
                "backend.app.services.auto_queue_ams.printer_manager.get_status", return_value=self._two_white_spools()
            ),
            patch("backend.app.services.auto_queue_ams.get_filament_requirements", return_value=requirements),
        ):
            return await compute_ams_mapping_for_printer(None, 7, item)

    async def test_force_override_lands_on_the_demanded_variant(self) -> None:
        """The matcher rejected every printer without Matte; dispatch must not
        then hand the job to the Basic spool sitting beside it."""
        item = self._item(
            [{"slot_id": 1, "type": "PLA", "color": "#FFFFFF", "tray_info_idx": MATTE, "force_color_match": True}]
        )
        reqs = [{"slot_id": 1, "type": "PLA", "color": "#FFFFFF", "tray_info_idx": MATTE, "used_grams": 10.0}]
        assert await self._map(item, reqs) == [1]

    async def test_force_override_falls_back_when_the_variant_is_absent(self) -> None:
        """An eligible printer never fails to map: no Silk loaded → type+colour."""
        item = self._item(
            [{"slot_id": 1, "type": "PLA", "color": "#FFFFFF", "tray_info_idx": "GFA06", "force_color_match": True}]
        )
        reqs = [{"slot_id": 1, "type": "PLA", "color": "#FFFFFF", "tray_info_idx": "GFA06", "used_grams": 10.0}]
        assert await self._map(item, reqs) == [0]

    async def test_preference_override_still_clears_the_variant(self) -> None:
        """A manual swap replaces the slot's filament, so the 3MF's idx now names
        the spool being replaced and must not pin anything."""
        item = self._item([{"slot_id": 1, "type": "PLA", "color": "#FFFFFF"}])
        reqs = [{"slot_id": 1, "type": "PLA", "color": "#FFFFFF", "tray_info_idx": MATTE, "used_grams": 10.0}]
        # Falls to exact-colour matching, which takes the first available tray.
        assert await self._map(item, reqs) == [0]


@pytest.mark.asyncio
class TestPerPrinterMapperHasNoOverrides:
    """``PrintQueueItem.filament_overrides`` was dropped by m002 together with
    model-based assignment — that whole idea lives in the auto-queue tier now.

    The scheduler kept reading the attribute anyway. Because ``run()`` wraps
    ``check_queue`` in a bare ``except Exception``, the AttributeError surfaced
    only as a one-line log and the item silently never dispatched — every pass,
    for as long as its stored mapping was missing or all-[-1].
    """

    async def test_computing_a_mapping_does_not_touch_a_column_we_do_not_have(self) -> None:
        item = PrintQueueItem()
        item.id = 1
        reqs = [{"slot_id": 1, "type": "PLA", "color": "#FFFFFF", "tray_info_idx": "", "used_grams": 10.0}]
        status = _mock_status(ams=[{"id": 0, "tray": [_white_pla(0, BASIC)]}])
        status.ams_filament_backup = None

        async def _reqs(_db, _item):
            return reqs

        async def _bool_setting(_db, _key):
            return False

        with (
            patch("backend.app.services.print_scheduler.printer_manager.get_status", return_value=status),
            patch.object(scheduler, "_get_filament_requirements", _reqs),
            patch.object(scheduler, "_get_bool_setting", _bool_setting),
        ):
            assert await scheduler._compute_ams_mapping_for_printer(None, 7, item) == [0]
