"""How much filament is left, decided once instead of three times.

Registry N4. The inventory sync, the Spoolman sync and the live status handler
each converted an AMS reading into grams, by copy. BS settles it in one place
(``DevAmsTray::get_filament_remain_weight``) with a precedence:

    remain_g >= 0  ->  the firmware's own grams win (0 means "nothing left")
    otherwise      ->  spool weight x remain% / 100

⚠️ **``remain_g`` is not a weight measurement, and expecting it to arrive is a
mistake.** No Bambu AMS weighs anything — the 2 Pro included. The number is
derived from the RFID tag plus how far the spool has turned, so it exists for
tagged spools and is ``-1`` everywhere else; a load cell is an open feature
request on Bambu's forum, not a product. This is here so the better number is
used the day it shows up, not because it is expected tomorrow.

⚠️ The percentage path stays because it is what nearly every tray provides, and
it is coarse in a way worth remembering: ``remain`` is an integer percent, so
one step is 10 g on a 1 kg spool.
"""

from __future__ import annotations

import pytest

from backend.app.utils.filament_remaining import grams_remaining, grams_used


class TestTheFirmwaresOwnGramsWin:
    def test_they_beat_the_percentage(self) -> None:
        """250 g reported against 50% of 1 kg — the percentage would say 500."""
        assert grams_remaining(250, 50, 1000) == 250.0

    def test_zero_grams_means_empty_not_missing(self) -> None:
        """⚠️ BS treats 0 as a real answer, and so must we: an empty spool is
        not the same as a spool nobody measured."""
        assert grams_remaining(0, 50, 1000) == 0.0

    def test_minus_one_is_the_absent_marker(self) -> None:
        assert grams_remaining(-1, 50, 1000) == 500.0

    def test_none_falls_through_as_well(self) -> None:
        assert grams_remaining(None, 50, 1000) == 500.0


class TestThePercentagePath:
    @pytest.mark.parametrize(("percent", "expected"), [(100, 1000.0), (50, 500.0), (0, 0.0)])
    def test_it_scales_the_label_weight(self, percent: int, expected: float) -> None:
        assert grams_remaining(-1, percent, 1000) == expected

    @pytest.mark.parametrize("percent", [-1, 101, None])
    def test_an_unusable_percentage_answers_nothing(self, percent) -> None:
        """⚠️ ``None``, not 0. A spool we know nothing about and an empty spool
        are different, and a caller that conflates them records a full spool as
        spent."""
        assert grams_remaining(-1, percent, 1000) is None

    @pytest.mark.parametrize("weight", [0, None, -5])
    def test_without_a_spool_weight_a_percentage_means_nothing(self, weight) -> None:
        """A percent of an unknown total is not a quantity."""
        assert grams_remaining(-1, 50, weight) is None


class TestUsedIsTheMirror:
    def test_it_is_what_the_spool_started_with_minus_what_is_left(self) -> None:
        assert grams_used(-1, 40, 1000) == 600.0

    def test_the_firmware_path_feeds_it_too(self) -> None:
        assert grams_used(250, 50, 1000) == 750.0

    def test_an_empty_spool_is_fully_used(self) -> None:
        assert grams_used(0, None, 1000) == 1000.0

    def test_it_needs_the_label_weight_even_with_firmware_grams(self) -> None:
        """⚠️ Firmware reports what is LEFT. "Used" exists only relative to what
        the spool began with, so a spool of unknown size has a knowable
        remainder and an unknowable consumption — and saying otherwise would
        invent a number."""
        assert grams_remaining(250, None, None) == 250.0
        assert grams_used(250, None, None) is None

    def test_it_never_goes_negative(self) -> None:
        """A tray reporting more than the label weight is a mislabelled spool,
        not negative consumption."""
        assert grams_used(1500, None, 1000) == 0.0
