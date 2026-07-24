"""Pure formula for a print archive's actual duration + estimate accuracy.

Single source of truth for the ``before_flush`` event in ``core/database.py`` that keeps
``PrintArchive.actual_time_seconds`` / ``time_accuracy`` in sync. The m107 migration's
raw-SQL backfill mirrors this exactly — keep the two in step if the clamp/rounding changes.
"""

from datetime import datetime


def _to_naive(dt: datetime | None) -> datetime | None:
    # DB stores naive datetimes (see core/database.py::_strip_tz_from_params); a
    # before_flush can hand us a naive started_at (loaded) next to an aware completed_at
    # (datetime.now(timezone.utc)), whose subtraction would raise.
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def compute_time_metrics(
    started_at: datetime | None,
    completed_at: datetime | None,
    status: str | None,
    print_time_seconds: int | None,
) -> tuple[int | None, float | None]:
    """Return ``(actual_time_seconds, time_accuracy)``.

    Actual = whole seconds of ``completed_at - started_at`` for **completed** prints with
    both timestamps and a positive duration; else ``None``. Accuracy =
    ``round(print_time_seconds / actual * 100, 1)`` kept only when the raw ratio is
    5..500%; else ``None``.
    """
    if status != "completed" or started_at is None or completed_at is None:
        return None, None
    actual = int((_to_naive(completed_at) - _to_naive(started_at)).total_seconds())
    if actual <= 0:
        return None, None
    accuracy: float | None = None
    if print_time_seconds and print_time_seconds > 0:
        raw = print_time_seconds / actual * 100
        if 5 <= raw <= 500:
            accuracy = round(raw, 1)
    return actual, accuracy
