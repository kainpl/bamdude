"""Scheduled AMS drying — the ONE writer of ``drying_schedules`` and ``scheduled_dryings``.

Spec: vault 60-specs/scheduled-drying-spec. Routes and ``PrintScheduler`` call
this module; nothing else writes the two tables. A rule's time is wall-clock in
the SERVER's timezone (``core/timezones.server_timezone()``): what the server does
on its own schedule belongs to the farm, not to whichever browser created it.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone, tzinfo

logger = logging.getLogger(__name__)

UTC = timezone.utc


def parse_hhmm(value: str) -> time:
    """ "HH:MM" → ``time``; exactly two digits each, 00:00–23:59."""
    parts = (value or "").split(":")
    if len(parts) != 2 or any(len(p) != 2 or not p.isdigit() for p in parts):
        raise ValueError(f"not an HH:MM time: {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        raise ValueError(f"not an HH:MM time: {value!r}")
    return time(hour, minute)


def as_aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def to_naive_utc(value: datetime) -> datetime:
    return as_aware_utc(value).replace(tzinfo=None)


def _local_instant(day: date, at: time, tz: tzinfo) -> datetime:
    """Wall clock ``at`` on ``day`` as an aware UTC instant.

    ``zoneinfo`` with ``fold=0`` resolves a time the clock skips (spring forward)
    with the offset before the gap — "03:30" becomes 04:30 — and an ambiguous
    one (autumn) as its first occurrence. Both are what a farm expects: the run
    still happens that night, once.
    """
    return datetime.combine(day, at, tzinfo=tz).astimezone(UTC)


def next_occurrence(start_time: str, weekdays: int, after: datetime, tz: tzinfo) -> datetime:
    """The first run of a rule strictly after ``after``, as aware UTC."""
    if not weekdays & 0b1111111:
        raise ValueError("weekdays selects no day")
    at = parse_hhmm(start_time)
    after = as_aware_utc(after)
    first_day = after.astimezone(tz).date()
    for offset in range(8):
        day = first_day + timedelta(days=offset)
        if not weekdays & (1 << day.weekday()):
            continue
        candidate = _local_instant(day, at, tz)
        if candidate > after:
            return candidate
    raise ValueError("no occurrence within a week")  # unreachable with a non-empty mask


def window_end(occurrence: datetime, start_time: str, latest_start: str | None, weekdays: int, tz: tzinfo) -> datetime:
    """The last moment a run may start: the rule's "not later than", else the next run."""
    occurrence = as_aware_utc(occurrence)
    if latest_start is None:
        return next_occurrence(start_time, weekdays, occurrence, tz)
    start_day = occurrence.astimezone(tz).date()
    latest = parse_hhmm(latest_start)
    end_day = start_day if latest > parse_hhmm(start_time) else start_day + timedelta(days=1)
    return _local_instant(end_day, latest, tz)
