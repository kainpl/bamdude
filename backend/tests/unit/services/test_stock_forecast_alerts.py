"""Stock-break alerts: the forecast that used to live only in a browser tab.

``on_stock_break_alert`` had a provider column, a Telegram toggle and en+uk
templates, and no caller — m059 said as much in its docstring and left the
trigger for "a future scheduled aggregator". The forecast deciding the alert ran
in ``ForecastPanel.tsx``, so the warning existed only while somebody had the
Inventory page open, which is exactly when it is least needed.

The arithmetic here is a port of the panel's, so most of what these tests pin is
*sameness*: the day-bucketed decayed rate, the baseline-aware fallback, the
lead-time comparison. The rest pins the part the panel never needed — when a
timer-driven alert is allowed to speak twice.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.filament_sku_settings import FilamentSkuSettings
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.stock_forecast_alerts import (
    StockForecastAlerts,
    delta_rate,
    find_stock_alerts,
    history_rate,
    sku_key,
)


async def _breaks(db_session, now):
    return [a for a in await find_stock_alerts(db_session, now) if a.kind == "break"]


async def _reorders(db_session, now):
    return [a for a in await find_stock_alerts(db_session, now) if a.kind == "reorder"]


NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def _record(days_ago: float, grams: float, spool_id: int = 1, anchor: datetime = NOW) -> SpoolUsageHistory:
    """Usage ``days_ago`` before ``anchor``.

    ``anchor`` exists because the two ways of driving this service disagree
    about what "now" is. Tests that call ``find_stock_alerts(db, NOW)`` pass the
    reference in, so data and evaluation share one clock. ``StockForecastAlerts.tick()``
    takes no such argument and reads the real one — so data anchored to the
    fixed ``NOW`` is measured against a wall clock that drifts further from it
    every hour the calendar advances. Anything going through ``tick()`` must
    anchor to the same real clock.
    """
    return SpoolUsageHistory(
        spool_id=spool_id,
        weight_used=grams,
        created_at=(anchor - timedelta(days=days_ago)).replace(tzinfo=None),
    )


async def _spool(db_session, **kwargs) -> Spool:
    defaults = {
        "material": "PLA",
        "brand": "Bambu",
        "color_name": "Black",
        "label_weight": 1000,
        "weight_used": 0.0,
        "weight_used_baseline": 0.0,
        "created_at": (NOW - timedelta(days=90)).replace(tzinfo=None),
    }
    defaults.update(kwargs)
    spool = Spool(**defaults)
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    return spool


async def _usage(db_session, spool_id: int, days_ago: float, grams: float, anchor: datetime = NOW) -> None:
    db_session.add(_record(days_ago, grams, spool_id, anchor))
    await db_session.commit()


class TestTheRate:
    def test_a_single_day_cannot_give_a_rate(self) -> None:
        """No gap to measure across — the delta fallback handles this."""
        assert history_rate([_record(1, 100), _record(1, 50)], NOW) is None

    def test_a_steady_hundred_grams_a_day(self) -> None:
        records = [_record(days_ago, 100) for days_ago in (5, 4, 3, 2, 1)]
        estimate = history_rate(records, NOW)
        assert estimate is not None
        assert estimate.rate == pytest.approx(100, rel=0.01)
        assert estimate.std_dev == pytest.approx(0, abs=0.01), "a perfectly steady rate has no spread"

    def test_prints_on_the_same_day_are_summed_not_treated_as_an_interval(self) -> None:
        """Two spools of one SKU printing minutes apart would otherwise produce a
        near-zero gap and a wildly inflated rate."""
        same_day = [_record(2, 50, spool_id=1), _record(2, 50, spool_id=2), _record(1, 100, spool_id=1)]
        assert history_rate(same_day, NOW).rate == pytest.approx(100, rel=0.01)

    def test_recent_prints_weigh_more_than_old_ones(self) -> None:
        """30-day half-life: a burst last week must outrank a quiet spell in
        spring, or the forecast lags reality by a season."""
        old_and_slow = [_record(d, 10) for d in (90, 89, 88, 87)]
        recent_and_fast = [_record(d, 200) for d in (3, 2, 1)]
        estimate = history_rate(old_and_slow + recent_and_fast, NOW)
        assert estimate is not None
        assert estimate.rate > 100, f"recent 200 g/day should dominate old 10 g/day, got {estimate.rate:.1f}"

    def test_the_delta_fallback_is_baseline_aware(self) -> None:
        """'Reset usage to 0' means the rate describes post-reset consumption."""
        spool = Spool(
            material="PLA",
            label_weight=1000,
            weight_used=900.0,
            weight_used_baseline=800.0,  # only 100 g counts
            created_at=(NOW - timedelta(days=10)).replace(tzinfo=None),
        )
        assert delta_rate([spool], NOW) == pytest.approx(10.0, rel=0.01)

    def test_a_spool_younger_than_a_day_gives_no_delta_rate(self) -> None:
        spool = Spool(
            material="PLA",
            label_weight=1000,
            weight_used=50.0,
            weight_used_baseline=0.0,
            created_at=(NOW - timedelta(hours=3)).replace(tzinfo=None),
        )
        assert delta_rate([spool], NOW) is None


class TestFindingBreaks:
    @staticmethod
    async def _steady_group(db_session, *, remaining: float, lead_time: int) -> Spool:
        """A SKU burning ~100 g/day with a known amount left."""
        db_session.add(Settings(key="forecast_global_lead_time_days", value=str(lead_time)))
        spool = await _spool(db_session, weight_used=1000 - remaining, label_weight=1000)
        for days_ago in (5, 4, 3, 2, 1):
            await _usage(db_session, spool.id, days_ago, 100)
        return spool

    @pytest.mark.asyncio
    async def test_running_out_inside_the_lead_time_is_a_break(self, db_session) -> None:
        await self._steady_group(db_session, remaining=300, lead_time=7)  # 3 days left, 7-day lead

        breaks = await _breaks(db_session, NOW)

        assert len(breaks) == 1
        assert breaks[0].material == "PLA"
        assert breaks[0].days_left == 3
        assert breaks[0].lead_time_days == 7
        assert breaks[0].stock_g == pytest.approx(300)

    @pytest.mark.asyncio
    async def test_comfortable_stock_is_not_a_break(self, db_session) -> None:
        await self._steady_group(db_session, remaining=900, lead_time=3)  # 9 days left, 3-day lead
        assert await _breaks(db_session, NOW) == []

    @pytest.mark.asyncio
    async def test_without_a_lead_time_there_is_nothing_to_be_early_about(self, db_session) -> None:
        """'Runs out before replenishment arrives' is meaningless when the
        operator has never said how long replenishment takes."""
        await self._steady_group(db_session, remaining=100, lead_time=0)
        assert await _breaks(db_session, NOW) == []

    @pytest.mark.asyncio
    async def test_a_snoozed_sku_stays_quiet(self, db_session) -> None:
        """Snooze on the panel has to mean snooze in the messages too."""
        await self._steady_group(db_session, remaining=100, lead_time=7)
        db_session.add(
            FilamentSkuSettings(material="PLA", subtype=None, brand="Bambu", color_name="Black", alerts_snoozed=True)
        )
        await db_session.commit()

        assert await _breaks(db_session, NOW) == []

    @pytest.mark.asyncio
    async def test_archived_spools_do_not_count_as_stock(self, db_session) -> None:
        spool = await self._steady_group(db_session, remaining=300, lead_time=7)
        # A full spool of the same SKU, but retired — it cannot be printed.
        await _spool(
            db_session,
            weight_used=0.0,
            archived_at=NOW.replace(tzinfo=None),
            created_at=spool.created_at,
        )

        breaks = await _breaks(db_session, NOW)
        assert len(breaks) == 1
        assert breaks[0].stock_g == pytest.approx(300), "the archived spool must not pad the stock"

    @pytest.mark.asyncio
    async def test_a_per_sku_lead_time_beats_a_shorter_global_one(self, db_session) -> None:
        await self._steady_group(db_session, remaining=500, lead_time=2)  # 5 days left, global 2
        db_session.add(
            FilamentSkuSettings(material="PLA", subtype=None, brand="Bambu", color_name="Black", lead_time_days=10)
        )
        await db_session.commit()

        breaks = await _breaks(db_session, NOW)
        assert len(breaks) == 1
        assert breaks[0].lead_time_days == 10

    @pytest.mark.asyncio
    async def test_a_colourless_settings_row_still_applies(self, db_session) -> None:
        """Rows written before forecasts became colour-aware carry the operator's
        lead time; the panel falls back to them and so must this."""
        await self._steady_group(db_session, remaining=500, lead_time=0)
        db_session.add(
            FilamentSkuSettings(material="PLA", subtype=None, brand="Bambu", color_name=None, lead_time_days=10)
        )
        await db_session.commit()

        breaks = await _breaks(db_session, NOW)
        assert len(breaks) == 1, "the colour-less row's 10-day lead time should apply to Black PLA"


class TestTheReorderPoint:
    """The earlier, softer sibling: stock still covers the lead time, but no
    longer covers it with the buffer. ``on_stock_reorder_alert`` was the fourth
    dead event and the best hidden — the comment above the method demonstrates
    the call, so searching for callers finds the documentation.
    """

    @staticmethod
    async def _steady_group(db_session, *, spools: int, remaining_each: float, lead_time: int) -> Spool:
        """A SKU burning ~100 g/day across ``spools`` spools."""
        db_session.add(Settings(key="forecast_global_lead_time_days", value=str(lead_time)))
        first = await _spool(db_session, weight_used=1000 - remaining_each)
        for days_ago in (5, 4, 3, 2, 1):
            await _usage(db_session, first.id, days_ago, 100)
        for _ in range(spools - 1):
            await _spool(db_session, weight_used=1000 - remaining_each)
        return first

    @pytest.mark.asyncio
    async def test_plenty_of_stock_raises_nothing(self, db_session) -> None:
        # 3000 g at 100 g/day = 30 days; reorder point is 7*100 + 14*100 = 2100.
        await self._steady_group(db_session, spools=3, remaining_each=1000, lead_time=7)
        assert await find_stock_alerts(db_session, NOW) == []

    @pytest.mark.asyncio
    async def test_falling_below_the_reorder_point_raises_a_reorder(self, db_session) -> None:
        # 900 g = 9 days, clear of the 7-day lead time, but under the 2100 g point.
        await self._steady_group(db_session, spools=1, remaining_each=900, lead_time=7)

        reorders = await _reorders(db_session, NOW)
        assert len(reorders) == 1
        assert reorders[0].days_left == 9

    @pytest.mark.asyncio
    async def test_a_stock_break_is_not_also_a_reorder(self, db_session) -> None:
        """Mutually exclusive, as on the panel: once the filament is going to run
        out in time, "you should reorder" is no longer the message worth sending.
        """
        await self._steady_group(db_session, spools=1, remaining_each=300, lead_time=7)

        alerts = await find_stock_alerts(db_session, NOW)
        assert [a.kind for a in alerts] == ["break"]

    @pytest.mark.asyncio
    async def test_a_margin_in_grams_is_taken_literally(self, db_session) -> None:
        """``safety_margin_unit='g'`` is already grams; multiplying it by the
        daily rate — as the 'days' branch does — would inflate the buffer
        twentyfold and make everything look like it needed reordering."""
        await self._steady_group(db_session, spools=1, remaining_each=900, lead_time=7)
        db_session.add(
            FilamentSkuSettings(
                material="PLA",
                subtype=None,
                brand="Bambu",
                color_name="Black",
                safety_margin_value=50,
                safety_margin_unit="g",
            )
        )
        await db_session.commit()

        # Point drops to 700 + 50 = 750, and 900 g is above it.
        assert await find_stock_alerts(db_session, NOW) == []

    @pytest.mark.asyncio
    async def test_snooze_covers_the_reorder_alert_too(self, db_session) -> None:
        await self._steady_group(db_session, spools=1, remaining_each=900, lead_time=7)
        db_session.add(
            FilamentSkuSettings(material="PLA", subtype=None, brand="Bambu", color_name="Black", alerts_snoozed=True)
        )
        await db_session.commit()

        assert await find_stock_alerts(db_session, NOW) == []


class TestTheTwoAlertsKeepSeparateBooks:
    @pytest.mark.asyncio
    async def test_a_reorder_uses_its_own_notification(self, db_session, monkeypatch) -> None:
        db_session.add(Settings(key="forecast_global_lead_time_days", value="7"))
        spool = await _spool(db_session, weight_used=100.0)  # 900 g left
        for days_ago in (5, 4, 3, 2, 1):
            await _usage(db_session, spool.id, days_ago, 100)

        reorder, breakage = AsyncMock(), AsyncMock()
        monkeypatch.setattr("backend.app.services.stock_forecast_alerts.async_session", _ctx(db_session))
        with (
            patch("backend.app.services.notification_service.notification_service.on_stock_reorder_alert", reorder),
            patch("backend.app.services.notification_service.notification_service.on_stock_break_alert", breakage),
        ):
            await StockForecastAlerts().tick()
            await StockForecastAlerts().tick()

        assert reorder.await_count == 1
        assert breakage.await_count == 0
        assert "lead_time_days" not in reorder.await_args.kwargs, "the reorder template has no lead-time placeholder"

    @pytest.mark.asyncio
    async def test_sliding_from_reorder_into_break_speaks_at_once(self, db_session, monkeypatch) -> None:
        """Sharing one stamp between the two states would silence a SKU for a day
        at the exact moment it got worse."""
        db_session.add(Settings(key="forecast_global_lead_time_days", value="7"))
        spool = await _spool(db_session, weight_used=100.0)  # 900 g → reorder
        for days_ago in (5, 4, 3, 2, 1):
            await _usage(db_session, spool.id, days_ago, 100)

        reorder, breakage = AsyncMock(), AsyncMock()
        monkeypatch.setattr("backend.app.services.stock_forecast_alerts.async_session", _ctx(db_session))
        with (
            patch("backend.app.services.notification_service.notification_service.on_stock_reorder_alert", reorder),
            patch("backend.app.services.notification_service.notification_service.on_stock_break_alert", breakage),
        ):
            await StockForecastAlerts().tick()
            spool.weight_used = 700.0  # 300 g → break
            await db_session.commit()
            await StockForecastAlerts().tick()

        assert reorder.await_count == 1
        assert breakage.await_count == 1, "the worse news must not wait out the reorder alert's repeat window"

        row = await _only_settings_row(db_session)
        assert row.stock_reorder_notified_at is None, "the state it left must be un-stamped"
        assert row.stock_break_notified_at is not None


class TestSayingItOnce:
    @staticmethod
    async def _sku_in_break(db_session) -> None:
        """A SKU in break, anchored to the clock ``tick()`` actually reads.

        Every test in this class goes through ``StockForecastAlerts.tick()``,
        which calls the real ``datetime.now()`` — it takes no reference to
        inject. Anchoring the usage to the fixed ``NOW`` therefore aged the data
        by however far the calendar had moved past 2026-07-30 12:00, growing
        every hour.

        350 g rather than 300 is the second half. ``days_left`` is
        ``math.floor(remaining_g / rate)``, and 300 g at 100 g/day lands on
        exactly 3.0 — a boundary where a rate a thousandth above 100 silently
        becomes 2. This suite ran green for a while and then began failing about
        half the time with ``assert 2 == 3``, on unchanged code. 350 g still
        yields 3 (``floor(3.5)``) and now needs a rate error above 14% to move,
        so it tests the behaviour rather than the rounding.
        """
        anchor = datetime.now(timezone.utc)
        db_session.add(Settings(key="forecast_global_lead_time_days", value="7"))
        spool = await _spool(db_session, weight_used=650.0)  # 350 g left
        for days_ago in (5, 4, 3, 2, 1):
            await _usage(db_session, spool.id, days_ago, 100, anchor)

    @pytest.mark.asyncio
    async def test_a_standing_break_is_announced_once_not_every_pass(self, db_session, monkeypatch) -> None:
        await self._sku_in_break(db_session)
        service = StockForecastAlerts()

        sent = AsyncMock()
        monkeypatch.setattr("backend.app.services.stock_forecast_alerts.async_session", _ctx(db_session))
        with patch("backend.app.services.notification_service.notification_service.on_stock_break_alert", sent):
            await service.tick()
            await service.tick()
            await service.tick()

        assert sent.await_count == 1

    @pytest.mark.asyncio
    async def test_it_repeats_after_a_day(self, db_session, monkeypatch) -> None:
        """The message says "order immediately"; a daily nudge is the point."""
        await self._sku_in_break(db_session)
        service = StockForecastAlerts()

        sent = AsyncMock()
        monkeypatch.setattr("backend.app.services.stock_forecast_alerts.async_session", _ctx(db_session))
        with patch("backend.app.services.notification_service.notification_service.on_stock_break_alert", sent):
            await service.tick()
            row = await _only_settings_row(db_session)
            row.stock_break_notified_at = (datetime.now(timezone.utc) - timedelta(days=2)).replace(tzinfo=None)
            await db_session.commit()
            await service.tick()

        assert sent.await_count == 2

    @pytest.mark.asyncio
    async def test_recovering_re_arms_the_alert(self, db_session, monkeypatch) -> None:
        """Restocking then running down again must announce at once, not wait out
        the repeat window."""
        await self._sku_in_break(db_session)
        service = StockForecastAlerts()

        sent = AsyncMock()
        monkeypatch.setattr("backend.app.services.stock_forecast_alerts.async_session", _ctx(db_session))
        with patch("backend.app.services.notification_service.notification_service.on_stock_break_alert", sent):
            await service.tick()

            spool = (await db_session.execute(_select_spools())).scalars().first()
            spool.weight_used = 0.0  # restocked
            await db_session.commit()
            await service.tick()

            row = await _only_settings_row(db_session)
            assert row.stock_break_notified_at is None, "recovery must clear the stamp"

            spool.weight_used = 700.0  # run down again
            await db_session.commit()
            await service.tick()

        assert sent.await_count == 2

    @pytest.mark.asyncio
    async def test_the_created_settings_row_carries_the_uis_own_defaults(self, db_session, monkeypatch) -> None:
        """A row appearing because of an alert must not change what the operator
        sees on the panel."""
        await self._sku_in_break(db_session)
        service = StockForecastAlerts()

        monkeypatch.setattr("backend.app.services.stock_forecast_alerts.async_session", _ctx(db_session))
        with patch("backend.app.services.notification_service.notification_service.on_stock_break_alert", AsyncMock()):
            await service.tick()

        row = await _only_settings_row(db_session)
        assert row.lead_time_days == 0
        assert row.safety_margin_value == 14
        assert row.safety_margin_unit == "days"
        assert row.alerts_snoozed is False

    @pytest.mark.asyncio
    async def test_a_failing_provider_does_not_become_a_message_every_six_hours(self, db_session, monkeypatch) -> None:
        """The stamp is written even when sending failed — a provider that comes
        back must not deliver the backlog."""
        await self._sku_in_break(db_session)
        service = StockForecastAlerts()

        boom = AsyncMock(side_effect=RuntimeError("telegram is down"))
        monkeypatch.setattr("backend.app.services.stock_forecast_alerts.async_session", _ctx(db_session))
        with patch("backend.app.services.notification_service.notification_service.on_stock_break_alert", boom):
            await service.tick()

        row = await _only_settings_row(db_session)
        assert row.stock_break_notified_at is not None

    @pytest.mark.asyncio
    async def test_the_message_carries_what_the_template_renders(self, db_session, monkeypatch) -> None:
        await self._sku_in_break(db_session)
        service = StockForecastAlerts()

        sent = AsyncMock()
        monkeypatch.setattr("backend.app.services.stock_forecast_alerts.async_session", _ctx(db_session))
        with patch("backend.app.services.notification_service.notification_service.on_stock_break_alert", sent):
            await service.tick()

        kwargs = sent.await_args.kwargs
        assert kwargs["material"] == "PLA"
        assert kwargs["brand"] == "Bambu"
        assert kwargs["stock_g"] == pytest.approx(350)
        assert kwargs["lead_time_days"] == 7
        assert kwargs["days_left"] == 3
        assert kwargs["rate_g_day"] > 0


def test_the_sku_key_collapses_nulls_like_the_panel_does() -> None:
    assert sku_key("PLA", None, None, None) == ("PLA", "", "", "")
    assert sku_key("PLA", "Matte", "Bambu", "Black") == ("PLA", "Matte", "Bambu", "Black")


def _ctx(db_session):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    return _session_ctx


def _select_spools():
    from sqlalchemy import select

    return select(Spool)


async def _only_settings_row(db_session) -> FilamentSkuSettings:
    from sqlalchemy import select

    return (await db_session.execute(select(FilamentSkuSettings))).scalars().one()
