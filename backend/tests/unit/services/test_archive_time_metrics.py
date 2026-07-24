"""Pure formula for a print archive's actual duration + estimate accuracy."""

from datetime import datetime, timedelta, timezone

from backend.app.services.archive_time_metrics import compute_time_metrics

S = datetime(2026, 1, 1, 10, 0, 0)


def test_completed_returns_actual_and_accuracy():
    actual, acc = compute_time_metrics(S, S + timedelta(seconds=7200), "completed", 7000)
    assert actual == 7200
    assert acc == round(7000 / 7200 * 100, 1)  # 97.2


def test_non_completed_returns_none():
    assert compute_time_metrics(S, S + timedelta(hours=1), "failed", 3600) == (None, None)
    assert compute_time_metrics(S, S + timedelta(hours=1), "printing", 3600) == (None, None)


def test_missing_timestamps_return_none():
    assert compute_time_metrics(None, S, "completed", 3600) == (None, None)
    assert compute_time_metrics(S, None, "completed", 3600) == (None, None)


def test_zero_or_negative_duration_none():
    assert compute_time_metrics(S, S, "completed", 3600) == (None, None)
    assert compute_time_metrics(S, S - timedelta(hours=1), "completed", 3600) == (None, None)


def test_accuracy_outside_range_dropped_actual_kept():
    # estimate 10x actual -> 1000% -> out of 5..500 -> accuracy None, actual kept
    actual, acc = compute_time_metrics(S, S + timedelta(seconds=100), "completed", 1000)
    assert actual == 100 and acc is None


def test_no_estimate_actual_only():
    actual, acc = compute_time_metrics(S, S + timedelta(seconds=100), "completed", None)
    assert actual == 100 and acc is None


def test_recovered_synthetic_is_100():
    # completed_at = started_at + estimate -> actual == estimate -> 100%
    actual, acc = compute_time_metrics(S, S + timedelta(seconds=3600), "completed", 3600)
    assert actual == 3600 and acc == 100.0


def test_mixed_tz_does_not_raise():
    aware_end = (S + timedelta(seconds=3600)).replace(tzinfo=timezone.utc)
    actual, acc = compute_time_metrics(S, aware_end, "completed", 3600)  # naive start, aware end
    assert actual == 3600 and acc == 100.0
