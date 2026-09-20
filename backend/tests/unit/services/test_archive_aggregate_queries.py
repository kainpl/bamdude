"""The aggregate against a seeded database.

The comparison is always against a hand-rolled fold over the same rows — the
point of the endpoint is that the numbers do not change, only where they are
computed.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.services.statistics import aggregate as agg

UTC = ZoneInfo("UTC")
KYIV = ZoneInfo("Europe/Kyiv")


def _archive(**kwargs) -> PrintArchive:
    """A minimally valid archive row; every test overrides what it cares about."""
    defaults = {
        "filename": "part.3mf",
        "file_path": "",
        "file_size": 0,
        "status": "completed",
        "quantity": 1,
        "created_at": datetime(2026, 9, 7, 12, 0),
    }
    return PrintArchive(**{**defaults, **kwargs})


async def _seed(db_session, rows: list[PrintArchive]) -> list[PrintArchive]:
    for row in rows:
        db_session.add(row)
    await db_session.commit()
    return rows


async def _seed_raw(db_session, **values) -> None:
    """Insert through Core, bypassing the ORM.

    ⚠️ ``actual_time_seconds`` is a DERIVED column: a ``before_flush`` listener
    recomputes it from ``completed_at - started_at`` on every flush, so a value
    handed to the ORM is overwritten before it reaches the database. Tests that
    need a specific stored duration — including shapes the current writer can no
    longer produce, like a legacy 0 — have to go around the ORM.
    """
    from sqlalchemy import insert

    defaults = {
        "filename": "part.3mf",
        "file_path": "",
        "file_size": 0,
        "status": "completed",
        "quantity": 1,
        "created_at": datetime(2026, 9, 7, 12, 0),
    }
    await db_session.execute(insert(PrintArchive).values(**{**defaults, **values}))
    await db_session.commit()


async def _collect(db_session, *, tz=UTC, date_from=None, date_to=None, user_id=None):
    return await agg.collect(db_session, tz=tz, date_from=date_from, date_to=date_to, user_id=user_id)


@pytest.mark.asyncio
async def test_buckets_agree_with_a_python_fold(db_session):
    rows = await _seed(
        db_session,
        [
            _archive(created_at=datetime(2026, 9, 7, 22, 30)),  # 8 Sep in Kyiv
            _archive(created_at=datetime(2026, 9, 7, 10, 0)),  # 7 Sep in Kyiv
            _archive(created_at=datetime(2026, 9, 7, 11, 0)),  # 7 Sep in Kyiv
        ],
    )
    out = await _collect(db_session, tz=KYIV)

    expected: dict[str, int] = {}
    for row in rows:
        key = row.created_at.replace(tzinfo=timezone.utc).astimezone(KYIV).strftime("%Y-%m-%d")
        expected[key] = expected.get(key, 0) + 1
    actual = {b["at"]: b["started"]["prints"] for b in out["buckets"] if b["started"]["prints"]}
    assert actual == expected
    assert out["timezone"] == "Europe/Kyiv"


@pytest.mark.asyncio
async def test_a_day_with_no_prints_is_still_a_bucket(db_session):
    await _seed(
        db_session,
        [_archive(created_at=datetime(2026, 9, 1, 12, 0)), _archive(created_at=datetime(2026, 9, 4, 12, 0))],
    )
    out = await _collect(db_session)
    assert [b["at"] for b in out["buckets"]] == ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    assert out["buckets"][1]["started"]["prints"] == 0


@pytest.mark.asyncio
async def test_the_two_axes_can_disagree(db_session):
    """A print created before midnight and finished after it belongs to one day
    on the started axis and the next on the ended axis — which is exactly why
    there are two."""
    await _seed(
        db_session,
        [_archive(created_at=datetime(2026, 9, 1, 23, 0), completed_at=datetime(2026, 9, 2, 1, 0))],
    )
    out = await _collect(db_session)
    by_day = {b["at"]: b for b in out["buckets"]}
    assert by_day["2026-09-01"]["started"]["prints"] == 1
    assert by_day["2026-09-01"]["ended"]["prints"] == 0
    assert by_day["2026-09-02"]["ended"]["prints"] == 1


@pytest.mark.asyncio
async def test_the_success_streak_matches_a_hand_rolled_run(db_session):
    """completed ×2, cancelled, completed ×3 → 3."""
    base = datetime(2026, 9, 1, 8, 0)
    statuses = ["completed", "completed", "cancelled", "completed", "completed", "completed"]
    await _seed(
        db_session,
        [_archive(created_at=base + timedelta(hours=i), status=s) for i, s in enumerate(statuses)],
    )
    out = await _collect(db_session)
    assert out["records"]["success_streak"] == 3


@pytest.mark.asyncio
async def test_the_streak_is_deterministic_when_two_prints_end_together(db_session):
    """⚠️ Verified against a real PostgreSQL 2026-09-08: without an explicit
    tiebreak the two dialects disagreed here, because the run length depends on
    which of two equally-timed rows the planner emits first."""
    same = datetime(2026, 9, 1, 8, 0)
    await _seed(
        db_session,
        [
            _archive(created_at=same, status="completed"),
            _archive(created_at=same, status="failed"),
            _archive(created_at=same, status="completed"),
        ],
    )
    out = await _collect(db_session)
    # Ordered by (ended, id): completed, failed, completed → the longest run is 1.
    assert out["records"]["success_streak"] == 1


@pytest.mark.asyncio
async def test_the_streak_is_zero_when_nothing_completed(db_session):
    await _seed(db_session, [_archive(status="failed"), _archive(status="cancelled")])
    out = await _collect(db_session)
    assert out["records"]["success_streak"] == 0


@pytest.mark.asyncio
async def test_materials_are_split_and_grams_divided(db_session):
    await _seed(
        db_session,
        [
            _archive(filament_type="PLA, PETG", filament_used_grams=100.0),
            _archive(filament_type="PLA", filament_used_grams=25.0),
        ],
    )
    out = await _collect(db_session)
    by = {r["material"]: r for r in out["by_material"]}
    assert by["PLA"]["prints"] == 2
    assert by["PETG"]["prints"] == 1
    assert by["PLA"]["grams"] == pytest.approx(75.0)  # 100/2 + 25
    assert by["PETG"]["grams"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_a_missing_material_is_unknown_and_a_missing_colour_is_absent(db_session):
    await _seed(db_session, [_archive(filament_type=None, filament_color=None, filament_used_grams=10.0)])
    out = await _collect(db_session)
    assert [r["material"] for r in out["by_material"]] == ["Unknown"]
    assert out["by_color"] == []


@pytest.mark.asyncio
async def test_an_archived_or_deleted_row_is_never_counted(db_session):
    await _seed(
        db_session,
        [
            _archive(),
            _archive(status="archived"),
            _archive(deleted_at=datetime(2026, 9, 7, 13, 0)),
        ],
    )
    out = await _collect(db_session)
    assert out["totals"]["prints"] == 1


@pytest.mark.asyncio
async def test_the_range_end_is_exclusive_of_the_next_local_day(db_session):
    """A print at 23:59 local on the last day is in; 00:01 the next day is out."""
    await _seed(
        db_session,
        [
            _archive(created_at=datetime(2026, 9, 1, 20, 59)),  # 23:59 Kyiv on the 1st
            _archive(created_at=datetime(2026, 9, 1, 21, 1)),  # 00:01 Kyiv on the 2nd
        ],
    )
    out = await _collect(db_session, tz=KYIV, date_from=date(2026, 9, 1), date_to=date(2026, 9, 1))
    assert out["totals"]["prints"] == 1


@pytest.mark.asyncio
async def test_hour_of_day_skips_rows_without_started_at(db_session):
    await _seed(
        db_session,
        [
            _archive(started_at=datetime(2026, 9, 7, 14, 0)),
            _archive(started_at=None),
        ],
    )
    out = await _collect(db_session)
    assert len(out["by_hour_of_day"]) == 24
    assert sum(c["prints"] for c in out["by_hour_of_day"]) == 1
    assert out["by_hour_of_day"][14]["prints"] == 1


@pytest.mark.asyncio
async def test_a_stored_zero_duration_falls_back_to_the_estimate(db_session):
    """⚠️ The frontend reads ``actual || print_time`` and 0 is falsy there, so a
    plain COALESCE would bucket this print as half an hour instead of three.

    Today's writer cannot produce a stored 0 — the before_flush listener returns
    None for a non-positive duration — so this goes in through Core to pin the
    behaviour for legacy rows and for the m107 backfill's shape.
    """
    await _seed_raw(db_session, actual_time_seconds=0, print_time_seconds=10800)
    out = await _collect(db_session)
    counts = {r["bucket"]: r["prints"] for r in out["by_duration"]}
    assert counts["2-4h"] == 1
    assert counts["<30m"] == 0


@pytest.mark.asyncio
async def test_duration_buckets_are_all_present_even_when_empty(db_session):
    await _seed(
        db_session,
        [_archive(started_at=datetime(2026, 9, 7, 8, 0), completed_at=datetime(2026, 9, 7, 9, 0))],
    )
    out = await _collect(db_session)
    assert [r["bucket"] for r in out["by_duration"]] == [name for name, _ in agg.DURATION_BUCKETS]
    assert {r["bucket"]: r["prints"] for r in out["by_duration"]}["30m-1h"] == 1


@pytest.mark.asyncio
async def test_records_name_the_winning_print(db_session):
    await _seed(
        db_session,
        [
            _archive(
                print_name="short",
                started_at=datetime(2026, 9, 7, 8, 0),
                completed_at=datetime(2026, 9, 7, 8, 10),
                filament_used_grams=5.0,
                cost=1.0,
            ),
            _archive(
                print_name="epic",
                started_at=datetime(2026, 9, 7, 8, 0),
                completed_at=datetime(2026, 9, 8, 9, 0),
                filament_used_grams=900.0,
                cost=40.0,
                energy_cost=5.0,
            ),
        ],
    )
    out = await _collect(db_session)
    assert out["records"]["longest"]["print_name"] == "epic"
    assert out["records"]["heaviest"]["print_name"] == "epic"
    assert out["records"]["costliest"]["print_name"] == "epic"
    assert out["records"]["costliest"]["total"] == pytest.approx(45.0)
    # The split is kept so the page can show "filament 40 + energy 5".
    assert out["records"]["costliest"]["cost"] == pytest.approx(40.0)
    assert out["records"]["costliest"]["energy_cost"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_a_record_only_counts_a_completed_print(db_session):
    """⚠️ A record is a claim about output. A twenty-hour run that was cancelled
    is not the longest print, and a failure that burned 900 g is not the
    heaviest — the page has always ranked completed prints only."""
    await _seed(
        db_session,
        [
            _archive(
                print_name="cancelled monster",
                status="cancelled",
                started_at=datetime(2026, 9, 7, 0, 0),
                completed_at=datetime(2026, 9, 8, 0, 0),
                filament_used_grams=900.0,
                cost=99.0,
            ),
            _archive(
                print_name="modest but finished",
                started_at=datetime(2026, 9, 7, 8, 0),
                completed_at=datetime(2026, 9, 7, 9, 0),
                filament_used_grams=20.0,
                cost=2.0,
            ),
        ],
    )
    out = await _collect(db_session)
    assert out["records"]["longest"]["print_name"] == "modest but finished"
    assert out["records"]["heaviest"]["print_name"] == "modest but finished"
    assert out["records"]["costliest"]["print_name"] == "modest but finished"


@pytest.mark.asyncio
async def test_an_empty_range_answers_zeros_and_no_winners(db_session):
    out = await _collect(db_session, date_from=date(1999, 1, 1), date_to=date(1999, 1, 2))
    assert out["totals"]["prints"] == 0
    assert out["buckets"] == []
    assert out["records"]["longest"] is None
    assert out["records"]["success_streak"] == 0
    assert len(out["by_hour_of_day"]) == 24


@pytest.mark.asyncio
async def test_the_owner_filter_hides_another_users_prints(db_session):
    await _seed(db_session, [_archive(created_by_id=1), _archive(created_by_id=2)])
    assert (await _collect(db_session))["totals"]["prints"] == 2
    assert (await _collect(db_session, user_id=2))["totals"]["prints"] == 1
