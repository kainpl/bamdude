"""The meter-stall detector (spec plug-meter-watchdog W1, W5; plan T3).

A suspicion, not a verdict: a lifetime counter that held one value for at least half an hour while the
power history of the same source covers that stretch well and adds up to at least the expected
energy. Pure: counter points and power points in, flat series out.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.services.meter_detector import CounterPoint, detect
from backend.app.services.power_integrator import Sample, normalize

T0 = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def power(watts: float, until: int = 3600, start: int = 0, step: int = 40):
    return normalize(Sample(at(t), watts) for t in range(start, until + 1, step))


def counters(*pairs):
    return [CounterPoint(at(s), kwh) for s, kwh in pairs]


def run(counter_points, points, min_wh=100.0):
    return detect(counter_points, points, gap_limit=180, edge_limit=180, min_wh=min_wh)


def test_a_flat_counter_under_200_w_for_an_hour_is_suspicious():
    """A01: the incident that started this — the counter stood still while the printer drew 200 W."""
    result = run(counters((0, 12.5), (3600, 12.5)), power(200.0))
    assert result.state == "suspicious"
    (series,) = result.suspicious
    assert (series.start, series.end) == (at(0), at(3600))
    assert series.integral.kwh == pytest.approx(0.2)


def test_three_minutes_at_120_w_is_not():
    """A02: 6 Wh can sit inside one 10 Wh step of the counter."""
    result = run(counters((0, 12.5), (180, 12.5)), power(120.0, until=180, step=10))
    assert result.state != "suspicious" and result.suspicious == []


def test_a_counter_that_moved_is_not():
    """A03."""
    result = run(counters((0, 12.5), (3600, 12.7)), power(200.0))
    assert result.state == "moving" and result.suspicious == []


def test_a_flat_tail_after_growth_is_found_on_its_own():
    """A04: a positive delta over the whole window does not hide the stretch where it stopped."""
    result = run(counters((0, 12.4), (1800, 12.5), (3600, 12.5), (7200, 12.5)), power(200.0, until=7200))
    assert result.state == "suspicious"
    (series,) = result.suspicious
    assert (series.start, series.end) == (at(1800), at(7200))


def test_a_counter_that_went_back_is_a_reset_not_a_stall():
    """A07."""
    result = run(counters((0, 12.5), (1500, 12.5), (3600, 0.3)), power(200.0))
    assert result.state == "reset"
    assert result.suspicious == []  # 25 flat minutes are short of the 30 a suspicion needs


def test_a_snapshot_gap_over_two_hours_proves_nothing():
    result = run(counters((0, 12.5), (3 * 3600, 12.5)), power(200.0, until=3 * 3600))
    assert result.suspicious == []


def test_poor_power_coverage_proves_nothing():
    """A05: half the hour without history is not «no consumption»."""
    result = run(counters((0, 12.5), (3600, 12.5)), power(200.0, until=1800))
    assert result.suspicious == [] and result.state == "insufficient"


def test_a_switched_off_plug_reporting_zero_is_not_suspicious():
    """A22: once a known OFF is written as 0 W, the night with a flat counter adds up to nothing."""
    result = run(counters((0, 12.5), (8 * 3600, 12.5)), power(0.0, until=8 * 3600))
    assert result.suspicious == []


def test_the_threshold_is_the_plugs_own_when_it_has_one():
    flat = counters((0, 12.5), (3600, 12.5))
    assert run(flat, power(200.0), min_wh=250.0).suspicious == []
    assert run(flat, power(300.0), min_wh=250.0).state == "suspicious"


def test_a_move_after_the_stall_is_the_recovery():
    """A08: only a new increase after the last flat point closes an episode."""
    result = run(counters((0, 12.5), (3600, 12.5), (5400, 12.6)), power(200.0, until=5400))
    assert result.state == "moving"
    assert len(result.suspicious) == 1
    assert result.recovered_at == at(5400)


def test_float_noise_does_not_break_a_flat_series():
    result = run(counters((0, 12.5), (1800, 12.5 + 1e-12), (3600, 12.5)), power(200.0))
    assert result.state == "suspicious"


# ---- review r1 MW-R1: an old hole must not hide a proven flat stretch ----------------------------


def test_clean_flat_suffix_survives_earlier_missing_history():
    """Three equal readings an hour apart; only the last hour has history, and it shows 200 Wh."""
    points = power(200.0, start=3600, until=7200, step=60)
    assert run(counters((3600, 10.0), (7200, 10.0)), points).suspicious  # the hour alone is proven
    result = run(counters((0, 10.0), (3600, 10.0), (7200, 10.0)), points)
    assert result.state == "suspicious"
    (series,) = result.suspicious
    # The run is the counter's flat stretch; the proof is the covered hour inside it.
    assert (series.start, series.end) == (at(0), at(7200))
    assert series.integral.kwh == pytest.approx(0.2)
    assert series.integral.sufficient


def test_a_proven_stretch_after_a_hole_in_the_middle_counts():
    points = power(200.0, start=0, until=1800, step=60) + power(200.0, start=5400, until=9000, step=60)
    result = run(counters((0, 10.0), (1800, 10.0), (5400, 10.0), (9000, 10.0)), points)
    assert result.state == "suspicious"
    assert result.suspicious[0].integral.kwh == pytest.approx(0.2)


def test_an_old_hole_beside_a_light_stretch_stays_no_signal():
    """The control: 20 Wh of covered history is a healthy quantized zero, whatever came before it."""
    points = power(20.0, start=3600, until=7200, step=60)
    result = run(counters((0, 10.0), (3600, 10.0), (7200, 10.0)), points)
    assert result.suspicious == []
    assert result.state != "suspicious"


def test_a_long_flat_day_with_holes_is_evaluated_within_budget():
    import time

    readings = [Sample(at(t), 200.0) for t in range(0, 24 * 3600 + 1, 10) if (t // 3600) % 3 != 0]
    points = normalize(readings)
    hourly = counters(*[(h * 3600, 10.0) for h in range(25)])

    started = time.perf_counter()
    result = run(hourly, points)
    elapsed = time.perf_counter() - started

    assert result.state == "suspicious"
    assert elapsed < 2.0, elapsed
