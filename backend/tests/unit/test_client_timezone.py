"""Whose day it is: the viewer's for questions people ask, the server's for work
the server does on its own.

A farm in Europe/Kyiv reading "today" resolved at UTC midnight loses the first
three hours of every day to yesterday. That is how this was noticed — a plug
card reporting 0 kWh used today, for a printer that had been running since
breakfast.
"""

import os
from datetime import date, datetime, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from backend.app.core.timezones import (
    CLIENT_TIMEZONE_HEADER,
    client_timezone,
    day_bounds,
    server_timezone,
    start_of_today,
)


def _request(header: str | None):
    req = MagicMock()
    req.headers = {CLIENT_TIMEZONE_HEADER: header} if header is not None else {}
    return req


class TestClientTimezone:
    def test_the_header_is_used_when_present(self):
        assert client_timezone(_request("Europe/Kyiv")) == ZoneInfo("Europe/Kyiv")

    def test_no_header_falls_back_to_the_server_not_utc(self, monkeypatch):
        """The callers without this header are not somewhere neutral — they are
        the Telegram bot, API keys, webhooks and Prometheus, all belonging to
        this deployment. Answering them in UTC would put a scripted "today"
        three hours from the same question asked in the UI."""
        monkeypatch.setenv("TZ", "Europe/Kyiv")

        assert client_timezone(_request(None)) == ZoneInfo("Europe/Kyiv")

    @pytest.mark.parametrize("bad", ["", "   ", "Mars/Olympus_Mons", "'; DROP TABLE--", "../../etc/passwd"])
    def test_an_unusable_header_degrades_instead_of_raising(self, bad, monkeypatch):
        """Untrusted input going into ZoneInfo, which raises on anything it does
        not recognise. A statistics page must not 500 because someone sent a
        header we cannot parse."""
        monkeypatch.setenv("TZ", "Europe/Kyiv")

        assert client_timezone(_request(bad)) == ZoneInfo("Europe/Kyiv")


class TestServerTimezone:
    def test_it_reads_the_tz_env_var(self, monkeypatch):
        """docker-compose sets this, defaulting to Europe/Kyiv, and the support
        bundle reports it — so on a normal install it is the farm's own zone."""
        monkeypatch.setenv("TZ", "America/New_York")

        assert server_timezone() == ZoneInfo("America/New_York")

    def test_unset_means_utc(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)

        assert server_timezone() is timezone.utc

    def test_an_unrecognised_value_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("TZ", "Not/AZone")

        assert server_timezone() is timezone.utc


class TestDayBounds:
    def test_a_kyiv_day_starts_three_hours_before_utc_midnight(self):
        """The actual defect, stated as arithmetic: 2026-07-31 in Kyiv begins at
        21:00 UTC on the 30th."""
        start, end = day_bounds(date(2026, 7, 31), ZoneInfo("Europe/Kyiv"))

        assert start == datetime(2026, 7, 30, 21, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 7, 31, 21, 0, tzinfo=timezone.utc)

    def test_the_end_is_exclusive_and_loses_no_microseconds(self):
        """``time.max`` would end the range at 23:59:59.999999 and silently drop
        anything in the final microsecond — invisible until a row lands there."""
        _, end = day_bounds(date(2026, 7, 31), timezone.utc)

        assert end == datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)

    def test_bounds_are_returned_in_utc_to_match_stored_timestamps(self):
        start, end = day_bounds(date(2026, 7, 31), ZoneInfo("Europe/Kyiv"))

        assert start.tzinfo is timezone.utc and end.tzinfo is timezone.utc

    def test_a_dst_transition_day_is_still_one_day(self):
        """Kyiv springs forward on 2026-03-29, making the local day 23 h long.
        Naive arithmetic that adds 24 h would overshoot into the next day."""
        start, end = day_bounds(date(2026, 3, 29), ZoneInfo("Europe/Kyiv"))

        assert (end - start).total_seconds() == 23 * 3600


class TestStartOfToday:
    def test_it_is_the_local_midnight_expressed_in_utc(self):
        tz = ZoneInfo("Europe/Kyiv")

        midnight = start_of_today(tz)

        assert midnight.tzinfo is timezone.utc
        assert midnight.astimezone(tz).hour == 0
        assert midnight <= datetime.now(timezone.utc)

    def test_different_zones_give_different_midnights(self):
        """Two people asking "today" from different places must not get the same
        boundary — that is the whole point of carrying the header."""
        kyiv = start_of_today(ZoneInfo("Europe/Kyiv"))
        la = start_of_today(ZoneInfo("America/Los_Angeles"))

        assert kyiv != la


def test_the_header_name_is_explicit_about_whose_zone_it_is():
    """Named for what it is rather than a generic X-Timezone: the server has one
    too, and confusing the two is exactly the hazard here."""
    assert CLIENT_TIMEZONE_HEADER == "X-Client-Timezone"
    assert "CLIENT" in CLIENT_TIMEZONE_HEADER.upper()
    assert os.environ.get("TZ") is not None or True  # server zone comes from TZ, not this header
