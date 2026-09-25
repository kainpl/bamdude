"""When a rule's runs start, in the server's timezone, across DST."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.app.services.scheduled_drying import next_occurrence, parse_hhmm, window_end

KYIV = ZoneInfo("Europe/Kyiv")
EVERY_DAY = 0b1111111
UTC = timezone.utc


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def test_parse_hhmm_rejects_garbage():
    assert parse_hhmm("01:00").hour == 1
    for bad in ("1:00", "24:00", "01:60", "0100", ""):
        with pytest.raises(ValueError):
            parse_hhmm(bad)


def test_today_if_still_ahead_else_tomorrow():
    # Kyiv is UTC+3 in September.
    assert next_occurrence("01:00", EVERY_DAY, utc(2026, 9, 24, 21, 59), KYIV) == utc(2026, 9, 24, 22, 0)
    assert next_occurrence("01:00", EVERY_DAY, utc(2026, 9, 24, 22, 0), KYIV) == utc(2026, 9, 25, 22, 0)


def test_weekdays_skip_unselected_days():
    # 2026-09-26 is a Saturday; its 01:00 Kyiv is 2026-09-25 22:00 UTC.
    saturday_only = 1 << 5
    assert next_occurrence("01:00", saturday_only, utc(2026, 9, 24, 23, 0), KYIV) == utc(2026, 9, 25, 22, 0)


def test_a_mask_selecting_no_day_is_refused():
    with pytest.raises(ValueError):
        next_occurrence("01:00", 0, utc(2026, 9, 24, 0, 0), KYIV)


def test_spring_forward_gap_runs_after_the_gap():
    # 2026-03-29 03:00 → 04:00 in Kyiv: "03:30" does not exist and resolves to 04:30 (01:30 UTC).
    assert next_occurrence("03:30", EVERY_DAY, utc(2026, 3, 28, 23, 0), KYIV) == utc(2026, 3, 29, 1, 30)


def test_autumn_overlap_takes_the_first_occurrence():
    # 2026-10-25 04:00 → 03:00 in Kyiv: "03:30" happens twice; the first is 00:30 UTC.
    assert next_occurrence("03:30", EVERY_DAY, utc(2026, 10, 24, 22, 0), KYIV) == utc(2026, 10, 25, 0, 30)


def test_the_next_night_is_not_lost_or_doubled_across_the_change():
    first = next_occurrence("01:00", EVERY_DAY, utc(2026, 10, 23, 23, 0), KYIV)
    second = next_occurrence("01:00", EVERY_DAY, first, KYIV)
    assert (first, second) == (utc(2026, 10, 24, 22, 0), utc(2026, 10, 25, 23, 0))


def test_window_crossing_midnight_ends_the_next_day():
    occ = utc(2026, 9, 25, 20, 0)  # 23:00 Kyiv
    assert window_end(occ, "23:00", "03:00", EVERY_DAY, KYIV) == utc(2026, 9, 26, 0, 0)


def test_no_window_waits_until_the_next_run():
    occ = utc(2026, 9, 24, 22, 0)
    assert window_end(occ, "01:00", None, EVERY_DAY, KYIV) == utc(2026, 9, 25, 22, 0)
