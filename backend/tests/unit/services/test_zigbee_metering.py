"""The scaling that turns a Zigbee counter into kWh.

Metering reports ``current_summ_delivered`` as an integer scaled by two sibling
attributes: ``kWh = raw × multiplier ÷ divisor``. Filing the raw value as kWh
yields a number wrong by whatever factor the device chose — typically 1000 or
10000 — which the per-print machinery then differences in good faith.

This is the same shape as the phase-0 bug where a daily counter was filed as a
lifetime total, so every print spanning midnight recorded negative consumption
and nothing looked broken until someone read the archive. A wrong number is
worse than no number, which is why a missing divisor yields None rather than a
convenient fallback.
"""

import pytest

from backend.app.services.zigbee.metering import scale


def test_typical_sonoff_scaling():
    """divisor 1000 is the common case, and what an S60ZBTPF-class plug reports."""
    assert scale(12345, multiplier=1, divisor=1000) == pytest.approx(12.345)


def test_multiplier_is_applied():
    assert scale(50, multiplier=10, divisor=100) == pytest.approx(5.0)


@pytest.mark.parametrize("divisor", [None, 0])
def test_no_divisor_means_no_reading(divisor):
    """A device that never said what its counter means has told us nothing.

    Falling back to 1 would invent a number and hand it to code that trusts it —
    the per-print delta would then be computed from raw counts and recorded as
    kilowatt-hours.
    """
    assert scale(12345, multiplier=1, divisor=divisor) is None


def test_missing_multiplier_defaults_to_one():
    """Asymmetric with the divisor, deliberately: 1 is the ZCL default for the
    multiplier and cannot make the result meaningless. A missing divisor can."""
    assert scale(2000, multiplier=None, divisor=1000) == pytest.approx(2.0)


def test_missing_raw_value_means_no_reading():
    assert scale(None, multiplier=1, divisor=1000) is None


def test_zero_is_a_real_reading_not_a_missing_one():
    """A plug that has never drawn power reports 0. Treating that as absent
    would show a healthy plug as unreadable."""
    assert scale(0, multiplier=1, divisor=1000) == 0.0


def test_large_counters_keep_their_precision():
    """Lifetime counters grow; the delta between two reads is what matters, so
    losing the low digits would quietly zero out short prints."""
    assert scale(987_654_321, multiplier=1, divisor=1000) == pytest.approx(987654.321)
