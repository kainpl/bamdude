"""A unique preset match is not a colour match (upstream #2687).

``tray_info_idx`` names the filament **variant**, not an individual spool:
GFA00 is PLA Basic, GFA01 PLA Matte, GFA17 PLA Translucent — *in every colour
Bambu sells*. The matcher nevertheless accepted a uniquely-matching idx as
definitive, on the premise "same preset = same spool = same colour".

So with one Matte spool loaded, every Matte requirement matched it whatever
colour it was, and the colour comparison was never reached. Upstream's reporter
saw the print dialog show **"(Ready)" with a green tick** for a slot wanting dark
red against a tray holding dark green — while picking that same tray by hand
reported the mismatch correctly, which is what exposed the inconsistency.

The asymmetry in our own code said the same thing: the "several trays share this
idx" branch already compared colour. Only uniqueness was trusted to imply it.

Ours had this in **three** places — these two matchers and the frontend hook —
where upstream had it in one. Variant still decides *selection* among trays that
agree on colour (#2650: PLA Basic is not PLA Matte); it just no longer decides
the verdict by itself.
"""

from __future__ import annotations

import pytest

from backend.app.services.auto_queue_ams import match_filaments_to_slots

MATTE = "GFA01"
BASIC = "GFA00"
RED = "#FF0000"
GREEN = "#00FF00"


def _tray(global_tray_id: int, color: str, idx: str, *, ftype: str = "PLA", remain: int = 50) -> dict:
    return {
        "global_tray_id": global_tray_id,
        "type": ftype,
        "color": color,
        "tray_info_idx": idx,
        "remain": remain,
    }


def _need(color: str, idx: str, *, ftype: str = "PLA") -> list[dict]:
    return [{"slot_id": 1, "type": ftype, "color": color, "tray_info_idx": idx}]


class TestVariantDoesNotOverrideColour:
    def test_a_correctly_coloured_tray_beats_the_wrong_coloured_variant(self) -> None:
        """The reported case. One Matte spool loaded, in green; the slice wants
        Matte red; a red Basic sits beside it. The red tray must win."""
        loaded = [_tray(0, GREEN, MATTE), _tray(1, RED, BASIC)]
        assert match_filaments_to_slots(_need(RED, MATTE), loaded) == [1]

    def test_the_variant_still_decides_among_trays_that_agree_on_colour(self) -> None:
        """#2650 is not undone: with both trays red, the Matte one is chosen."""
        loaded = [_tray(0, RED, MATTE), _tray(1, RED, BASIC)]
        assert match_filaments_to_slots(_need(RED, MATTE), loaded) == [0]

    def test_the_wrong_coloured_variant_is_still_used_when_nothing_else_exists(self) -> None:
        """A last resort beats mapping nothing at all — the print can still run,
        and the dialog is now honest about the colour."""
        assert match_filaments_to_slots(_need(RED, MATTE), [_tray(0, GREEN, MATTE)]) == [0]

    def test_a_similar_colour_of_another_variant_beats_a_wildly_wrong_variant_match(self) -> None:
        """The perceptual tolerance still applies, and still outranks the
        right-variant-wrong-colour fallback."""
        near_red = "#F50505"
        loaded = [_tray(0, GREEN, MATTE), _tray(1, near_red, BASIC)]
        assert match_filaments_to_slots(_need(RED, MATTE), loaded) == [1]


class TestUnchangedBehaviour:
    def test_an_exact_idx_and_colour_match_is_picked(self) -> None:
        loaded = [_tray(0, GREEN, BASIC), _tray(1, RED, MATTE)]
        assert match_filaments_to_slots(_need(RED, MATTE), loaded) == [1]

    def test_a_requirement_with_no_idx_matches_on_colour_alone(self) -> None:
        loaded = [_tray(0, GREEN, BASIC), _tray(1, RED, BASIC)]
        assert match_filaments_to_slots(_need(RED, ""), loaded) == [1]

    def test_a_type_mismatch_still_maps_nothing(self) -> None:
        loaded = [_tray(0, RED, MATTE, ftype="PETG")]
        assert match_filaments_to_slots(_need(RED, MATTE, ftype="TPU"), loaded) == [-1]

    def test_two_slots_do_not_share_one_tray(self) -> None:
        loaded = [_tray(0, RED, MATTE), _tray(1, RED, MATTE)]
        reqs = [
            {"slot_id": 1, "type": "PLA", "color": RED, "tray_info_idx": MATTE},
            {"slot_id": 2, "type": "PLA", "color": RED, "tray_info_idx": MATTE},
        ]
        assert sorted(match_filaments_to_slots(reqs, loaded)) == [0, 1]


class TestPreferLowest:
    @pytest.mark.parametrize("prefer_lowest", [False, True])
    def test_colour_still_outranks_the_remaining_preference(self, prefer_lowest: bool) -> None:
        """prefer_lowest reorders candidates; it must not promote a wrong colour."""
        loaded = [_tray(0, GREEN, MATTE, remain=5), _tray(1, RED, MATTE, remain=90)]
        assert match_filaments_to_slots(_need(RED, MATTE), loaded, prefer_lowest=prefer_lowest) == [1]
