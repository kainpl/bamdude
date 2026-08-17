"""How much filament is left, decided once instead of three times.

Registry N4. The inventory sync, the Spoolman sync and the live status handler
each converted an AMS reading into grams, by copy. BS settles it in one place
(``DevAmsTray::get_filament_remain_weight``, ``DeviceCore/DevFilaSystem.cpp``):

    if (remain_g >= 0) { return remain_g > 0 ? remain_g : nullopt; }
    weight_int = stoi(weight) * remain / 100;
    return weight_int > 0 ? weight_int : nullopt;

⚠️ **The three tests that used to assert 0 was an answer are gone, and this file
is the record of why.** They read the first line of that function and stopped,
concluding "0 grams is an ANSWER, not its absence, as in BS". BS's next token is
``remain_g > 0 ? ... : nullopt`` — it declines on zero, on both paths, always.
The tests were green, and what they pinned was a live-fire bug: a single AMS
push reporting ``remain: 0`` two seconds after an MQTT reconnect wrote three 1 kg
X2D spools off as fully consumed (m138 repairs the rows).

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

from backend.app.utils.filament_remaining import grams_remaining, grams_used, usable_remain_percent


class TestTheFirmwaresOwnGramsWin:
    def test_they_beat_the_percentage(self) -> None:
        """250 g reported against 50% of 1 kg — the percentage would say 500."""
        assert grams_remaining(250, 50, 1000) == 250.0

    def test_minus_one_is_the_absent_marker(self) -> None:
        assert grams_remaining(-1, 50, 1000) == 500.0

    def test_none_falls_through_as_well(self) -> None:
        assert grams_remaining(None, 50, 1000) == 500.0


class TestZeroIsASentinel:
    """⚠️ The heart of it. BS answers ``nullopt`` for every zero, and so do we."""

    def test_zero_grams_is_not_an_empty_spool(self) -> None:
        assert grams_remaining(0, None, 1000) is None

    def test_zero_grams_does_not_fall_through_to_the_percentage(self) -> None:
        """⚠️ Surprising, and deliberate: BS short-circuits on the field being
        PRESENT (``remain_g >= 0``), not on it being useful. A tray offering
        ``remain_g: 0`` alongside a healthy 50% is answered ``None``, not 500 —
        because the two disagree and the firmware's own field is the one BS
        consults first."""
        assert grams_remaining(0, 50, 1000) is None

    def test_zero_percent_is_not_an_empty_spool_either(self) -> None:
        """BS's percentage branch ends in ``weight_int > 0 ? ... : nullopt``."""
        assert grams_remaining(-1, 0, 1000) is None

    def test_and_so_no_reading_can_declare_a_spool_fully_used(self) -> None:
        """The whole point, stated as consumption: this is the assertion whose
        absence let one MQTT reconnect spend three spools."""
        assert grams_used(-1, 0, 1000) is None
        assert grams_used(0, -1, 1000) is None
        assert grams_used(0, 50, 1000) is None


class TestThePercentagePath:
    @pytest.mark.parametrize(("percent", "expected"), [(100, 1000.0), (50, 500.0), (1, 10.0)])
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


class TestTheDeltaPathsAskTheSameQuestion:
    """``usable_remain_percent`` — for the two remain%-delta fallbacks, which
    never see a label weight and only subtract one reading from another."""

    @pytest.mark.parametrize("percent", [1, 40, 100])
    def test_a_real_percentage_passes_through(self, percent: int) -> None:
        assert usable_remain_percent(percent) == percent

    def test_zero_is_refused_here_too(self) -> None:
        """⚠️ The same sentinel rule, and the reason it matters most here: a
        delta charges ``start - current``, so a zero at the completion end bills
        the whole reel."""
        assert usable_remain_percent(0) is None

    @pytest.mark.parametrize("value", [-1, 101, None, "50", 50.0])
    def test_anything_that_is_not_a_percentage_is_refused(self, value) -> None:
        assert usable_remain_percent(value) is None

    def test_a_bool_is_not_a_reading(self) -> None:
        """``True`` is an ``int`` in Python and would sail through as 1%."""
        assert usable_remain_percent(True) is None
        assert usable_remain_percent(False) is None
