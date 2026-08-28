"""A soak the chamber has already had should not be paid for twice.

Ported from the half of upstream #2727 we took. Back-to-back prints in
chamber-heated materials (ASA, ABS, PA, PC) each paid a full heat-soak from
cold, even when the print that just finished had left the chamber at
temperature.

⚠️ Every uncertain case must fail towards the FULL soak. Crediting a soak that
did not happen starts a print on a cold chamber, which is a warped part;
refusing to credit one that did costs some minutes.

(Upstream's other half — holding the bed hot between prints — is deliberately
not here. A hot bed is what you wait out to release a part, and the plastics
that need a chamber must not be cooled outside one anyway.)
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from backend.app.services import chamber_history

SOAK = 300
TARGET = 50.0


@pytest.fixture(autouse=True)
def _clean():
    chamber_history._history.clear()
    yield
    chamber_history._history.clear()


def _seed(printer_id: int, readings: list[tuple[float, float]]) -> None:
    """Seed history directly as (seconds ago, celsius), newest last."""
    now = time.monotonic()
    chamber_history._history[printer_id] = __import__("collections").deque((now - ago, temp) for ago, temp in readings)


def _steady(minutes: float, temp: float = 55.0, step: float = 30.0) -> list[tuple[float, float]]:
    """A run of samples at ``temp``, ``step`` seconds apart, ending now."""
    total = int(minutes * 60)
    return [(float(ago), temp) for ago in range(total, -1, -int(step))]


class TestCreditingWhatHappened:
    def test_a_chamber_held_at_temperature_skips_the_soak(self):
        _seed(1, _steady(20))

        assert chamber_history.soak_remaining(1, TARGET, SOAK) == 0

    def test_a_shorter_stretch_shortens_it_rather_than_skipping(self):
        """⚠️ The general case, and the reason this is a credit and not a
        boolean fast path: two minutes at temperature is worth two minutes."""
        _seed(1, _steady(2))

        remaining = chamber_history.soak_remaining(1, TARGET, SOAK)
        assert 150 < remaining < 200, remaining

    def test_a_reading_inside_the_tolerance_still_counts(self):
        _seed(1, _steady(20, temp=TARGET - 1.5))

        assert chamber_history.soak_remaining(1, TARGET, SOAK) == 0


class TestRefusingToCredit:
    def test_no_history_at_all(self):
        assert chamber_history.soak_remaining(1, TARGET, SOAK) == SOAK

    def test_a_chamber_below_target_right_now(self):
        _seed(1, _steady(20) + [(0.0, 30.0)])

        assert chamber_history.soak_remaining(1, TARGET, SOAK) == SOAK

    def test_a_stale_history(self):
        """⚠️ Two hours of readings whose newest is half an hour old is evidence
        of nothing: at the measured cooling rate the chamber can cross the
        threshold unobserved."""
        _seed(1, [(1800.0 + ago, 55.0) for ago in range(600, -1, -30)])

        assert chamber_history.soak_remaining(1, TARGET, SOAK) == SOAK

    def test_only_the_most_recent_unbroken_run_counts(self):
        """A gap is a disconnect, and time on its far side is not evidence."""
        old_run = [(float(ago), 55.0) for ago in range(3600, 3000, -30)]
        recent = _steady(1)
        _seed(1, old_run + recent)

        remaining = chamber_history.soak_remaining(1, TARGET, SOAK)
        assert remaining >= SOAK - 90, remaining


class TestDipsMustLastToCount:
    def test_a_brief_dip_is_noise_and_does_not_reset_the_credit(self):
        """⚠️ A plate change produces exactly this. An enclosed chamber cannot
        lose and regain several degrees in a minute — that is a door opening or
        a sensor artifact, not lost soak."""
        samples = _steady(20)
        # ⚠️ The stray reading has to be RECENT for this to prove anything. Put
        # it ten minutes back and there is still more than a full soak of good
        # readings after it, so the test passes with the grace period removed
        # entirely — measured.
        samples = [(ago, 30.0 if ago == 60.0 else temp) for ago, temp in samples]
        _seed(1, samples)

        assert chamber_history.soak_remaining(1, TARGET, SOAK) == 0

    def test_a_dip_that_outlasts_the_grace_period_does_reset_it(self):
        """Ten minutes below target is the chamber genuinely cooling.

        ⚠️ The dip has to close RECENTLY for this to prove anything. An earlier
        version put the recovery twelve minutes back, which leaves more good
        readings than the whole soak — so it credited everything and passed
        with the grace period ignored entirely."""
        samples = _steady(30)
        samples = [(ago, 30.0 if 120.0 <= ago <= 720.0 else temp) for ago, temp in samples]
        _seed(1, samples)

        remaining = chamber_history.soak_remaining(1, TARGET, SOAK)
        assert 150 < remaining < 250, remaining

    def test_the_credit_resumes_at_the_dip_not_at_the_start(self):
        """Time since the chamber came back is still time at temperature."""
        samples = _steady(60)
        samples = [(ago, 30.0 if 2000.0 <= ago <= 3000.0 else temp) for ago, temp in samples]
        _seed(1, samples)

        # ~33 minutes of good readings since the dip closed — well past the soak.
        assert chamber_history.soak_remaining(1, TARGET, SOAK) == 0


class TestSampling:
    def test_a_connected_printer_with_a_chamber_is_recorded(self):
        chamber_history.sample_all({1: SimpleNamespace(connected=True, temperatures={"chamber": 48.0})})

        assert len(chamber_history._history[1]) == 1

    def test_a_disconnected_printer_is_not(self):
        chamber_history.sample_all({1: SimpleNamespace(connected=False, temperatures={"chamber": 48.0})})

        assert 1 not in chamber_history._history

    def test_a_printer_with_no_chamber_sensor_is_not(self):
        chamber_history.sample_all({1: SimpleNamespace(connected=True, temperatures={"bed": 60.0})})

        assert 1 not in chamber_history._history

    def test_a_printer_that_has_gone_away_loses_its_history(self):
        """Otherwise a deleted printer's readings outlive it, and an id reused
        by a new one would inherit them."""
        chamber_history.record(1, 55.0)

        chamber_history.sample_all({})

        assert 1 not in chamber_history._history

    def test_an_unreadable_reading_is_skipped_not_fatal(self):
        chamber_history.sample_all({1: SimpleNamespace(connected=True, temperatures={"chamber": "warm"})})

        assert 1 not in chamber_history._history

    def test_the_window_is_bounded(self):
        chamber_history._history[1] = __import__("collections").deque([(time.monotonic() - 3 * 60 * 60, 55.0)])

        chamber_history.record(1, 55.0)

        assert len(chamber_history._history[1]) == 1
