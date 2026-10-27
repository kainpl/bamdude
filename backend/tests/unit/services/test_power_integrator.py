"""The one integrator of plug power history (spec plug-meter-watchdog W3, W4; plan T2).

Pure: samples in, energy and its quality out. The detector and the archive estimate both read it, so
what counts as a usable sample, a covered stretch and enough history is decided here once.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.services.power_integrator import Sample, integrate, normalize

T0 = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def every(step: int, watts, until: int = 3600, start: int = 0, reason=None) -> list[Sample]:
    return [Sample(at(t), watts(t) if callable(watts) else watts, reason) for t in range(start, until + 1, step)]


def run(samples, start=0, end=3600, gap=180, edge=180):
    return integrate(normalize(samples), at(start), at(end), gap_limit=gap, edge_limit=edge)


class TestEnergy:
    def test_a_steady_200_w_for_an_hour_is_0_2_kwh(self):
        result = run(every(40, 200.0))
        assert result.sufficient
        assert result.kwh == pytest.approx(0.2)
        assert result.coverage == pytest.approx(1.0)

    def test_a_ramp_from_0_to_400_w_over_an_hour_is_0_2_kwh(self):
        assert run(every(60, lambda t: 400.0 * t / 3600)).kwh == pytest.approx(0.2)

    def test_the_window_clips_a_segment_by_linear_interpolation(self):
        # 0 W at -60 s, 120 W at +60 s: inside [0, 3600] only from 0 (60 W) to 60 s (120 W).
        samples = [Sample(at(-60), 0.0), Sample(at(60), 120.0), *every(60, 120.0, start=120)]
        result = run(samples)
        first_minute_wh = (60 + 120) / 2 * 60 / 3600
        rest_wh = 120.0 * (3600 - 60) / 3600
        assert result.kwh * 1000 == pytest.approx(first_minute_wh + rest_wh)

    def test_naive_and_aware_and_offset_timestamps_of_one_moment_are_one_point(self):
        naive = (T0 + timedelta(seconds=60)).replace(tzinfo=None)
        plus3 = (T0 + timedelta(seconds=60)).astimezone(timezone(timedelta(hours=3)))
        points = normalize([Sample(naive, 100.0), Sample(at(60), 100.0), Sample(plus3, 100.0)])
        assert len(points) == 1 and points[0].usable


class TestGaps:
    def test_a_gap_longer_than_the_limit_is_not_bridged(self):
        samples = every(60, 200.0, until=1200) + every(60, 200.0, start=1500)
        result = run(samples)
        assert result.covered_seconds == pytest.approx(3600 - 300)
        assert result.max_gap == pytest.approx(300)

    def test_a_gap_of_exactly_the_limit_is_bridged(self):
        assert run(every(180, 200.0)).coverage == pytest.approx(1.0)

    def test_nothing_is_carried_forward_or_rescaled(self):
        result = run(every(60, 200.0, until=1800), gap=180)
        assert result.kwh == pytest.approx(0.1)  # only the covered half, never stretched to the hour
        assert not result.sufficient
        assert "coverage" in result.reasons

    def test_no_samples_is_insufficient_not_zero(self):
        result = run([])
        assert not result.sufficient and result.kwh == 0.0 and "no_samples" in result.reasons


class TestQuality:
    def test_ninety_five_percent_with_short_edges_is_enough(self):
        # covered from 90 s to 3510 s: 95 % exactly, each edge 90 s.
        result = run(every(30, 200.0, start=90, until=3510))
        assert result.coverage == pytest.approx(0.95)
        assert result.sufficient

    def test_one_step_less_coverage_is_not(self):
        result = run(every(1, 200.0, start=91, until=3510), gap=180)
        assert not result.sufficient

    def test_an_uncovered_edge_longer_than_its_limit_is_not_enough(self):
        result = run(every(60, 200.0, start=0, until=3420), edge=120)
        assert result.coverage == pytest.approx(0.95)
        assert not result.sufficient and "edge_tail" in result.reasons

    def test_a_300_s_cadence_with_its_own_limit_covers_an_aligned_hour(self):
        result = run(every(300, 200.0), gap=900, edge=900)
        assert result.sufficient and result.kwh == pytest.approx(0.2)

    def test_the_same_cadence_out_of_phase_is_insufficient_never_extrapolated(self):
        result = run(every(300, 200.0, start=240, until=3540), gap=900, edge=900)
        assert result.coverage == pytest.approx(3300 / 3600)
        assert not result.sufficient


class TestUnusable:
    def test_a_marked_moment_stops_both_segments_that_touch_it(self):
        samples = every(60, 200.0)
        samples[10] = Sample(samples[10].at, 0.0, "state_unknown")
        result = run(samples)
        assert result.covered_seconds == pytest.approx(3600 - 120)

    def test_a_valid_duplicate_does_not_rescue_a_marked_moment(self):
        samples = every(60, 200.0) + [Sample(at(600), 0.0, "batch_unknown")]
        result = run(samples)
        assert result.covered_seconds == pytest.approx(3600 - 120)

    def test_an_unknown_reason_is_unusable_too(self):
        samples = every(60, 200.0) + [Sample(at(600), 200.0, "something_new")]
        assert run(samples).covered_seconds == pytest.approx(3600 - 120)

    def test_twenty_short_rejections_never_pass_as_coverage(self):
        """Consilium r3: dropping them would bridge 20 x 10 Wh at a 100 % coverage."""
        samples = every(180, 200.0)
        for k in range(20):
            for s in (45, 90, 135):
                samples.append(Sample(at(k * 180 + s), 0.0, "state_unknown"))
        result = run(samples)
        assert result.covered_seconds == 0.0 and not result.sufficient

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -5.0, True])
    def test_an_invalid_value_is_an_unusable_moment(self, bad):
        samples = every(60, 200.0) + [Sample(at(630), bad)]
        assert run(samples).covered_seconds == pytest.approx(3600 - 60)

    def test_two_different_values_at_one_moment_are_a_conflict_not_a_choice(self):
        samples = every(60, 200.0) + [Sample(at(600), 150.0)]
        assert run(samples).covered_seconds == pytest.approx(3600 - 120)


class TestWhatAnEstimateCannotKnow:
    """A27, A31: the integral is of the readings, not of the load; neither the 100 Wh threshold nor an
    estimate is a guarantee of the energy drawn."""

    def test_short_peaks_landing_on_every_reading_are_integrated_as_a_steady_load(self):
        # An idle plug whose one-second 2000 W peaks happen to coincide with its minute readings:
        # the history says 2 kWh for an hour that drew about 33 Wh. Aliasing -- what the samples show.
        result = run(every(60, 2000.0))
        drawn_kwh = 2000.0 * 1 * 60 / 3600 / 1000
        assert result.sufficient
        assert result.kwh == pytest.approx(2.0)
        assert result.kwh > 50 * drawn_kwh

    def test_one_bridge_of_90_s_at_600_w_is_15_wh_whatever_was_drawn_between(self):
        # Two readings 90 s apart, nothing between: inside the gap limit, so the line is drawn. Its
        # trapezoid is G x P = 15 Wh -- the bound of one bridge, and already above 10 Wh.
        result = run([Sample(at(0), 600.0), Sample(at(90), 600.0)], end=90)
        assert result.sufficient
        assert result.kwh * 1000 == pytest.approx(600.0 * 90 / 3600)
        assert result.kwh * 1000 > 10.0
