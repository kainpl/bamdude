"""The aggregate's pure half: bucket arithmetic, local folding, the split rule.

⚠️ Bucketing is 15 minutes, not an hour, so that a zone with a :30 or :45 offset
still has every bucket land whole inside one local day and one local hour.
Folding happens in zoneinfo, so it is DST-correct on both dialects.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.app.services.statistics import aggregate as agg


def _index(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp()) // agg.BUCKET_SECONDS


def test_a_bucket_folds_to_the_local_day():
    kyiv = ZoneInfo("Europe/Kyiv")
    # 22:30 UTC on 7 Sep is 01:30 on 8 Sep in Kyiv (UTC+3, summer).
    idx = _index(datetime(2026, 9, 7, 22, 30))
    assert agg.local_key(idx, kyiv, "day") == "2026-09-08"
    assert agg.local_key(idx, kyiv, "hour") == "2026-09-08T01"
    assert agg.local_hour_of_day(idx, kyiv) == 1


def test_folding_survives_a_dst_change():
    """Kyiv leaves DST on Sunday 25 Oct 2026: +3 before, +2 after.

    ⚠️ 21:30 UTC is chosen deliberately — it is an hour at which the two offsets
    give DIFFERENT local days, so a fixed-offset implementation fails this test.
    24 Oct 21:30 UTC is 25 Oct 00:30 local (+3); 25 Oct 21:30 UTC is 25 Oct 23:30
    local (+2). Two different UTC days folding onto the same local day.
    """
    kyiv = ZoneInfo("Europe/Kyiv")
    before = _index(datetime(2026, 10, 24, 21, 30))
    after = _index(datetime(2026, 10, 25, 21, 30))
    assert agg.local_key(before, kyiv, "day") == "2026-10-25"
    assert agg.local_key(after, kyiv, "day") == "2026-10-25"


def test_a_half_hour_zone_lands_whole_inside_a_local_day():
    kolkata = ZoneInfo("Asia/Kolkata")  # UTC+5:30
    idx = _index(datetime(2026, 9, 7, 18, 45))  # 00:15 local on the 8th
    assert agg.local_key(idx, kolkata, "day") == "2026-09-08"
    assert agg.local_key(idx, kolkata, "hour") == "2026-09-08T00"


@pytest.mark.parametrize(
    ("date_from", "date_to", "expected"),
    [
        (date(2026, 9, 1), date(2026, 9, 7), "hour"),  # exactly 7 days
        (date(2026, 9, 1), date(2026, 9, 8), "day"),  # 8 days
        (date(2026, 9, 1), None, "day"),  # an open end is never hourly
        (None, None, "day"),
    ],
)
def test_granularity_switches_at_seven_days(date_from, date_to, expected):
    assert agg.granularity_for(date_from, date_to) == expected


def test_gaps_are_filled_so_the_client_never_has_to():
    assert agg.fill_gaps({"2026-09-01", "2026-09-04"}, "day") == [
        "2026-09-01",
        "2026-09-02",
        "2026-09-03",
        "2026-09-04",
    ]
    assert agg.fill_gaps({"2026-09-01T22", "2026-09-02T01"}, "hour") == [
        "2026-09-01T22",
        "2026-09-01T23",
        "2026-09-02T00",
        "2026-09-02T01",
    ]
    assert agg.fill_gaps([], "day") == []


def test_the_split_rule_matches_the_frontend():
    """Grams divide, counts do not — the rule StatsPage has always used."""
    rows = [
        {"key": "PLA, PETG", "prints": 2, "grams": 100.0, "seconds": 3600.0, "completed": 2, "failed": 0},
        {"key": "PLA", "prints": 1, "grams": 50.0, "seconds": 1800.0, "completed": 0, "failed": 1},
    ]
    out = {
        r["key"]: r
        for r in agg.split_multi(
            rows,
            sep=", ",
            default="Unknown",
            divide=("grams", "seconds"),
            whole=("prints", "completed", "failed"),
        )
    }
    assert out["PLA"]["grams"] == pytest.approx(100.0)  # 100/2 + 50
    assert out["PETG"]["grams"] == pytest.approx(50.0)
    assert out["PLA"]["prints"] == 3  # whole, from both rows
    assert out["PETG"]["prints"] == 2
    assert out["PLA"]["failed"] == 1
    assert out["PLA"]["seconds"] == pytest.approx(3600.0)  # 1800 + 1800


def test_an_empty_material_becomes_unknown_but_an_empty_colour_is_dropped():
    materials = agg.split_multi(
        [{"key": None, "prints": 1, "grams": 10.0}],
        sep=", ",
        default="Unknown",
        divide=("grams",),
        whole=("prints",),
    )
    assert materials[0]["key"] == "Unknown"
    assert (
        agg.split_multi(
            [{"key": None, "prints": 1, "grams": 10.0}],
            sep=",",
            default=None,
            divide=("grams",),
            whole=("prints",),
        )
        == []
    )


def test_colours_are_split_on_a_bare_comma_and_trimmed():
    out = {
        r["key"]: r
        for r in agg.split_multi(
            [{"key": "#FF0000, #00FF00", "prints": 1, "grams": 30.0}],
            sep=",",
            default=None,
            divide=("grams",),
            whole=("prints",),
        )
    }
    assert set(out) == {"#FF0000", "#00FF00"}
    assert out["#FF0000"]["grams"] == pytest.approx(15.0)
    assert out["#FF0000"]["prints"] == 1


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (1, "<30m"),
        (1800, "<30m"),
        (1801, "30m-1h"),
        (86400, "12-24h"),
        (86401, "24h+"),
        (0, None),
        (None, None),
        (-5, None),
    ],
)
def test_duration_buckets_match_the_frontend_boundaries(seconds, expected):
    assert agg.duration_bucket(seconds) == expected
