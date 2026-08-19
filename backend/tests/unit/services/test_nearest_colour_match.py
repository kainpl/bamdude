"""Among the spools the tolerance admits, take the nearest — not the first.

Ported from upstream #2804 / #2823, with #2804's follow-up ranking (CIEDE2000
rather than RGB) folded in rather than shipped and then corrected.

⚠️ Tray order is the order spools happen to sit in the AMS. It has nothing to do
with colour, so "first eligible wins" meant a print asking for a dark green took
whichever near-enough spool was in the lower slot even with a visibly closer one
two slots along.

⚠️ Eligibility is untouched. Which trays qualify is still the RGB tolerance in
``_colors_are_similar``; this only reorders trays that already qualified. Widening
what counts as a match is a different decision from choosing among the matches.
"""

from __future__ import annotations

import pytest

import backend.app.models.printer_location  # noqa: F401
from backend.app.services.print_scheduler import PrintScheduler


@pytest.fixture
def scheduler():
    return PrintScheduler()


def _tray(tray_id: int, colour: str, ftype: str = "PLA") -> dict:
    return {"global_tray_id": tray_id, "type": ftype, "color": colour, "tray_info_idx": ""}


def _need(colour: str, ftype: str = "PLA", slot: int = 1) -> dict:
    return {"slot_id": slot, "type": ftype, "color": colour, "tray_info_idx": ""}


class TestChoosingAmongEligibleTrays:
    def test_the_nearest_wins_even_from_a_later_slot(self, scheduler):
        """The whole point. Tray 1 qualifies; tray 2 qualifies and looks closer."""
        mapping = scheduler._match_filaments_to_slots(
            [_need("30A050")],
            [_tray(1, "1E9040"), _tray(2, "2FA04F")],
        )

        assert mapping == [2]

    def test_and_from_an_earlier_slot_when_that_is_the_nearer_one(self, scheduler):
        """Not simply reversed — the ranking has to actually rank."""
        mapping = scheduler._match_filaments_to_slots(
            [_need("30A050")],
            [_tray(1, "2FA04F"), _tray(2, "1E9040")],
        )

        assert mapping == [1]

    def test_an_exact_match_still_outranks_every_near_one(self, scheduler):
        """⚠️ Ranking near matches must not let one overtake an exact hit."""
        mapping = scheduler._match_filaments_to_slots(
            [_need("30A050")],
            [_tray(1, "2FA04F"), _tray(2, "30A050")],
        )

        assert mapping == [2]

    def test_a_near_match_still_outranks_a_type_only_one(self, scheduler):
        mapping = scheduler._match_filaments_to_slots(
            [_need("30A050")],
            [_tray(1, "FF0000"), _tray(2, "2FA04F")],
        )

        assert mapping == [2]


class TestEligibilityIsUnchanged:
    def test_a_colour_outside_the_tolerance_is_not_promoted_by_ranking(self, scheduler):
        """Being the *closest* of the far ones does not make a spool eligible;
        it is still a last-resort type-only match, and any near one beats it."""
        mapping = scheduler._match_filaments_to_slots(
            [_need("30A050")],
            [_tray(1, "0000FF"), _tray(2, "2FA04F")],
        )

        assert mapping == [2]

    def test_with_nothing_near_it_still_falls_back_to_type(self, scheduler):
        mapping = scheduler._match_filaments_to_slots(
            [_need("30A050")],
            [_tray(1, "0000FF")],
        )

        assert mapping == [1]

    def test_a_wrong_type_is_never_matched_however_close_the_colour(self, scheduler):
        mapping = scheduler._match_filaments_to_slots(
            [_need("30A050", ftype="PETG")],
            [_tray(1, "30A050", ftype="PLA")],
        )

        assert mapping == [-1]


class TestTraysWithNoReadableColour:
    def test_one_loses_to_a_readable_candidate(self, scheduler):
        """Unreadable is not "far", but it is not evidence of nearness either."""
        mapping = scheduler._match_filaments_to_slots(
            [_need("30A050")],
            [_tray(1, "2FA04F"), _tray(2, "")],
        )

        assert mapping == [1]

    def test_the_matcher_does_not_crash_on_one(self, scheduler):
        mapping = scheduler._match_filaments_to_slots([_need("30A050")], [_tray(1, "")])

        assert mapping in ([1], [-1])


class TestSeveralSlots:
    def test_each_slot_takes_its_own_nearest_and_no_tray_is_reused(self, scheduler):
        mapping = scheduler._match_filaments_to_slots(
            [_need("30A050", slot=1), _need("A03050", slot=2)],
            [_tray(1, "2FA04F"), _tray(2, "9F2F4F")],
        )

        assert mapping == [1, 2]
