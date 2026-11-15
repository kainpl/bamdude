"""The fallback (delta) rate counts use, not shelf time (upstream 1d86cff5a, #3407).

Until two distinct days of usage history exist, a SKU's daily rate is its
spools' consumed grams over time. The old divisor was the days since the
OLDEST spool row was created, and the dividend every gram ever consumed:

* ten spools bought in January and first loaded in June were forecast at a
  fraction of their real rate — the months on the shelf counted as use;
* a SKU printed for two years was averaged over two years, although every
  other tier of the engine looks at 90 days.

Each spool's consumption is now spread from its first use (its earliest usage
record, else its creation) to its last (``last_used``, else its archiving, else
now), and what falls inside the 90-day window is divided by the days of that
window — which starts no earlier than the SKU's first use.

The pure function is pinned by the golden vectors (``rate_vectors.json``); the
tests here walk the same scenarios through the SQL layer.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from backend.tests.test_forecast_engine import (  # noqa: F401 — fixtures and seeding helpers
    _PLA_BLACK,
    NOW,
    _naive,
    _rows_by_key,
    _spool,
    _usage,
    engine,
)


class TestShelfTimeIsNotUse:
    @pytest.mark.asyncio
    async def test_a_spool_bought_long_ago_and_opened_ten_days_ago(self, engine, db_session):
        """100 g in the 10 days since it was first printed with is 10 g/day —
        not 100 g over the 150 days since it was entered (0.67 g/day)."""
        spool = await _spool(db_session, weight_used=100.0, created_at=_naive(NOW - timedelta(days=150)))
        await _usage(db_session, spool.id, days_ago=10, grams=100.0)  # one day: no history tier

        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.rate_tier == "delta"
        assert row.rate_g_day == pytest.approx(10.0, rel=1e-9)
        assert row.days_remaining == 90  # 900 g left at 10 g/day

    @pytest.mark.asyncio
    async def test_unopened_spools_bought_with_it_do_not_slow_the_rate(self, engine, db_session):
        opened = await _spool(db_session, weight_used=200.0, created_at=_naive(NOW - timedelta(days=150)))
        await _usage(db_session, opened.id, days_ago=20, grams=200.0)
        for _ in range(3):
            await _spool(db_session, created_at=_naive(NOW - timedelta(days=150)))

        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.rate_g_day == pytest.approx(10.0, rel=1e-9)  # 200 g over 20 days
        assert row.total_spools == 4


class TestTheRateIsCountedOverTheWindow:
    @pytest.mark.asyncio
    async def test_use_that_ended_before_the_window_is_no_rate_now(self, engine, db_session):
        """Printed a year ago, untouched since: nothing was used in the last 90 days."""
        await _spool(
            db_session,
            weight_used=600.0,
            created_at=_naive(NOW - timedelta(days=400)),
            last_used=_naive(NOW - timedelta(days=300)),
        )
        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.rate_tier == "delta"
        assert row.rate_g_day == 0.0
        assert row.days_remaining is None and not row.reorder_alert and not row.stock_break_alert

    @pytest.mark.asyncio
    async def test_a_span_across_the_window_counts_its_inside_part(self, engine, db_session):
        """900 g used evenly from day -135 to day -45: half of it inside the window,
        450 g over 90 days = 5 g/day (lifetime average would say 6.67)."""
        await _spool(
            db_session,
            weight_used=900.0,
            label_weight=2000,
            created_at=_naive(NOW - timedelta(days=135)),
            last_used=_naive(NOW - timedelta(days=45)),
        )
        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.rate_g_day == pytest.approx(5.0, rel=1e-9)

    @pytest.mark.asyncio
    async def test_a_spool_used_up_long_ago_says_the_sku_was_in_use_all_along(self, engine, db_session):
        """One spool emptied 200 days ago, a new one opened 10 days ago: the SKU has
        been in use for longer than the window, so 100 g is 100 g per 90 days."""
        await _spool(
            db_session,
            weight_used=1000.0,
            created_at=_naive(NOW - timedelta(days=300)),
            last_used=_naive(NOW - timedelta(days=200)),
            archived_at=_naive(NOW - timedelta(days=200)),
        )
        new = await _spool(db_session, weight_used=100.0, created_at=_naive(NOW - timedelta(days=30)))
        await _usage(db_session, new.id, days_ago=10, grams=100.0)

        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.rate_g_day == pytest.approx(100.0 / 90.0, rel=1e-9)

    @pytest.mark.asyncio
    async def test_a_spool_used_up_in_one_day_counts_whole_at_that_day(self, engine, db_session):
        spool = await _spool(
            db_session,
            weight_used=200.0,
            created_at=_naive(NOW - timedelta(days=30)),
            last_used=_naive(NOW - timedelta(days=5) + timedelta(hours=2)),
        )
        await _usage(db_session, spool.id, days_ago=5, grams=200.0)
        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.rate_g_day == pytest.approx(40.0, rel=1e-9)  # 200 g, observed for 5 days


class TestWhatDidNotMove:
    @pytest.mark.asyncio
    async def test_a_spool_in_use_since_it_was_entered_reads_as_before(self, engine, db_session):
        await _spool(db_session, weight_used=100.0, created_at=_naive(NOW - timedelta(days=10)))
        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.rate_g_day == pytest.approx(10.0, rel=1e-9)

    @pytest.mark.asyncio
    async def test_a_reset_spool_still_counts_only_what_it_used_since(self, engine, db_session):
        await _spool(
            db_session,
            weight_used=900.0,
            weight_used_baseline=800.0,
            created_at=_naive(NOW - timedelta(days=10)),
        )
        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.rate_g_day == pytest.approx(10.0, rel=1e-9)
