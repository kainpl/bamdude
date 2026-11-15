"""The stock forecast in Spoolman mode (upstream 1d86cff5a, #3407; guard of 704919339).

One engine, two spool sources. In Spoolman mode ``forecast_engine`` builds its
SKU groups from the Spoolman spool list (stock, consumption, first and last
use) and from the Spoolman spools' own usage rows (``spoolman_usage_history``)
— and finishes them with the same functions as the built-in inventory, so the
golden vectors stay the arbiter in both modes.

The two inventories number their spools independently. Nothing of one may reach
the other's forecast: a Spoolman spool whose id equals a leftover built-in
spool's must not take over its prints (upstream 704919339), and the built-in
forecast never reads a Spoolman row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.filament_sku_settings import FilamentSkuSettings
from backend.app.models.settings import Settings
from backend.app.models.spoolman_usage_history import SpoolmanUsageHistory
from backend.app.services import forecast_engine, spoolman_usage_history as history
from backend.app.services.filament_needs import Needs
from backend.app.services.stock_forecast_alerts import StockForecastAlerts, find_stock_alerts
from backend.tests.test_forecast_engine import (  # noqa: F401 — fixtures and seeding helpers
    NOW,
    _history_vector,
    _naive,
    _seed_vector_usage,
    _spool,
    _usage,
    engine,
)

REGISTERED = "2026-06-01T08:00:00Z"
_KEY = ("PLA", "Matte", "Sunlu", "Matte")  # no colour name in Spoolman: the subtype stands in


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sm_spool(spool_id, *, used=0.0, weight=1000, registered=REGISTERED, archived=False, **over):
    """A Spoolman spool row, shaped as its API returns one."""
    spool = {
        "id": spool_id,
        "registered": registered,
        "initial_weight": weight,
        "used_weight": used,
        "remaining_weight": weight - used,
        "archived": archived,
        "first_used": None,
        "last_used": None,
        "filament": {
            "name": "PLA Matte",
            "material": "PLA",
            "color_hex": "112233",
            "weight": weight,
            "vendor": {"name": "Sunlu"},
        },
        "extra": {},
    }
    spool.update(over)
    return spool


@pytest.fixture
async def spoolman_mode(db_session):
    db_session.add(Settings(key="spoolman_enabled", value="true"))
    db_session.add(Settings(key="spoolman_url", value="http://localhost:7912"))
    await db_session.commit()


@pytest.fixture
def spoolman(monkeypatch):
    """What Spoolman answers the engine's spool-list read. ``spoolman.spools`` is the list."""

    class _Spoolman:
        spools: list[dict] = []
        calls = 0
        down = False

    state = _Spoolman()
    state.spools = []

    async def _fetch(_db):
        state.calls += 1
        if state.down:
            raise forecast_engine.ForecastSourceUnavailable("Spoolman server is not reachable")
        return list(state.spools)

    monkeypatch.setattr(forecast_engine, "_fetch_spoolman_spools", _fetch)
    return state


async def _sm_usage(db_session, spool, days_ago: float, grams: float, *, token=None, registered="same") -> None:
    token = token or await history.source_token(db_session)
    db_session.add(
        SpoolmanUsageHistory(
            source_token=token,
            spoolman_spool_id=spool["id"],
            spool_registered=spool["registered"] if registered == "same" else registered,
            weight_used=grams,
            percent_used=0,
            status="completed",
            created_at=_naive(NOW - timedelta(days=days_ago)),
        )
    )
    await db_session.commit()


async def _rows(db_session) -> dict[tuple, object]:
    rows = await forecast_engine.compute_forecast(db_session, now=NOW, reserved=Needs())
    return {(r.material, r.subtype, r.brand, r.color_name): r for r in rows}


class TestTheStockComesFromSpoolman:
    @pytest.mark.asyncio
    async def test_live_spools_are_the_stock_and_archived_ones_only_consumption(
        self, db_session, spoolman_mode, spoolman
    ):
        spoolman.spools = [
            _sm_spool(1, used=250.0),
            _sm_spool(2),
            _sm_spool(3, used=1000.0, archived=True, last_used=_iso(5)),
        ]
        row = (await _rows(db_session))[_KEY]
        assert (row.total_spools, row.total_remaining_g, row.total_label_g) == (2, 1750.0, 2000.0)
        assert row.total_used_g == 1250.0
        assert row.spool_ids == [1, 2]
        assert row.avg_spool_label_g == 1000.0
        assert row.rgba == "112233FF"

    @pytest.mark.asyncio
    async def test_without_usage_rows_the_rate_is_the_estimate_from_first_and_last_use(
        self, db_session, spoolman_mode, spoolman
    ):
        """Spoolman's own first_used / last_used: 200 g over the 20 days it was in use."""
        spoolman.spools = [_sm_spool(1, used=200.0, first_used=_iso(20), last_used=_iso(0))]
        row = (await _rows(db_session))[_KEY]
        assert row.rate_tier == "delta"
        assert row.rate_g_day == pytest.approx(10.0, rel=1e-9)
        assert row.days_remaining == 80

    @pytest.mark.asyncio
    async def test_the_local_spool_table_is_not_the_inventory(self, db_session, spoolman_mode, spoolman):
        await _spool(db_session, material="ABS", weight_used=500.0)
        spoolman.spools = [_sm_spool(1)]
        assert set(await _rows(db_session)) == {_KEY}


class TestTheTrendComesFromTheSpoolmanRows:
    @pytest.mark.asyncio
    async def test_the_same_usage_gives_the_same_rate_as_the_built_in_inventory(
        self, db_session, spoolman_mode, spoolman
    ):
        """Vector parity: one golden history vector, replayed as Spoolman rows."""
        vector = _history_vector("six_events_across_four_days_mixed_grams")
        spools = {sid: _sm_spool(sid, used=100.0) for sid in (1, 2, 3)}
        spoolman.spools = list(spools.values())
        token = await history.source_token(db_session)
        for record in vector["records"]:
            db_session.add(
                SpoolmanUsageHistory(
                    source_token=token,
                    spoolman_spool_id=record["spool_id"],
                    spool_registered=REGISTERED,
                    weight_used=record["weight_used"],
                    percent_used=0,
                    status="completed",
                    created_at=datetime.fromisoformat(record["created_at"]),
                )
            )
        await db_session.commit()

        row = (await _rows(db_session))[_KEY]
        assert row.rate_tier == "history"
        assert row.rate_g_day == pytest.approx(vector["expected"]["rate"], rel=1e-9)
        assert row.std_dev == pytest.approx(vector["expected"]["std_dev"], rel=1e-9)

    @pytest.mark.asyncio
    async def test_a_built_in_spool_with_the_same_id_lends_nothing(self, db_session, spoolman_mode, spoolman):
        """Upstream 704919339: built-in spool 1 printed 46 g/day before Spoolman was switched on."""
        local = await _spool(db_session, material="PLA", subtype="Matte", brand="Sunlu", color_name="Matte")
        assert local.id == 1
        for days_ago in (4, 3, 2, 1):
            await _usage(db_session, local.id, days_ago, 46.0)
        spoolman.spools = [_sm_spool(1, used=25.0, first_used=_iso(10), last_used=_iso(0))]

        row = (await _rows(db_session))[_KEY]
        assert row.rate_tier == "delta"
        assert row.rate_g_day == pytest.approx(2.5, rel=1e-9)

    @pytest.mark.asyncio
    async def test_rows_of_an_earlier_holder_of_the_id_are_not_the_spools(self, db_session, spoolman_mode, spoolman):
        """Spool 1 was deleted in Spoolman and its id given to a spool registered 3 days ago."""
        old = _sm_spool(1)
        for days_ago in (30, 29, 28):
            await _sm_usage(db_session, old, days_ago, 300.0)
        new = _sm_spool(1, used=20.0, registered=_iso(3), first_used=_iso(2), last_used=_iso(0))
        spoolman.spools = [new]

        row = (await _rows(db_session))[_KEY]
        assert row.rate_tier == "delta"
        assert row.rate_g_day == pytest.approx(10.0, rel=1e-9)  # 20 g over its own 2 days

    @pytest.mark.asyncio
    async def test_rows_without_a_stamp_are_judged_by_their_age(self, db_session, spoolman_mode, spoolman):
        new = _sm_spool(1, used=20.0, registered=_iso(3), first_used=_iso(2), last_used=_iso(0))
        for days_ago in (30, 29):
            await _sm_usage(db_session, new, days_ago, 300.0, registered=None)
        spoolman.spools = [new]
        assert (await _rows(db_session))[_KEY].rate_tier == "delta"

    @pytest.mark.asyncio
    async def test_rows_of_a_previous_spoolman_address_are_not_read(self, db_session, spoolman_mode, spoolman):
        spool = _sm_spool(1, used=20.0, first_used=_iso(2), last_used=_iso(0))
        for days_ago in (6, 5, 4):
            await _sm_usage(db_session, spool, days_ago, 300.0, token="a-previous-source")
        spoolman.spools = [spool]
        assert (await _rows(db_session))[_KEY].rate_tier == "delta"

    @pytest.mark.asyncio
    async def test_the_chart_series_is_the_spoolman_rows_by_day(self, db_session, spoolman_mode, spoolman):
        spool = _sm_spool(1, used=70.0)
        spoolman.spools = [spool]
        await _sm_usage(db_session, spool, 2.0, 30.0)
        await _sm_usage(db_session, spool, 2.0, 15.0)
        await _sm_usage(db_session, spool, 1.0, 25.0)
        series = await forecast_engine.usage_day_series(db_session, sku_keys=[_KEY], days=30, now=NOW)
        assert [grams for _day, grams in series[_KEY]] == [45.0, 25.0]


class TestTheBuiltInForecastNeverReadsSpoolman:
    @pytest.mark.asyncio
    async def test_spoolman_rows_and_spoolman_itself_are_left_alone(self, db_session, spoolman):
        """Switched back to the built-in inventory: the rows stay in the table, unread."""
        db_session.add(Settings(key="spoolman_enabled", value="false"))
        db_session.add(Settings(key="spoolman_url", value="http://localhost:7912"))
        await db_session.commit()
        spool = _sm_spool(1)
        for days_ago in (3, 2, 1):
            await _sm_usage(db_session, spool, days_ago, 500.0)
        local = await _spool(db_session, weight_used=100.0, created_at=_naive(NOW - timedelta(days=10)))
        assert local.id == 1

        rows = await _rows(db_session)
        row = rows[("PLA", None, "Bambu", "Black")]
        assert set(rows) == {("PLA", None, "Bambu", "Black")}
        assert (row.rate_tier, row.rate_g_day) == ("delta", pytest.approx(10.0))
        assert spoolman.calls == 0


class TestSpoolmanNotAnswering:
    @pytest.mark.asyncio
    async def test_the_engine_refuses_rather_than_forecast_an_empty_shelf(self, db_session, spoolman_mode, spoolman):
        spoolman.down = True
        with pytest.raises(forecast_engine.ForecastSourceUnavailable):
            await forecast_engine.compute_forecast(db_session, now=NOW, reserved=Needs())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/forecast", "/forecast/chart?days=30"])
    async def test_the_routes_answer_a_typed_refusal(
        self, async_client: AsyncClient, db_session, spoolman_mode, spoolman, path
    ):
        spoolman.down = True
        response = await async_client.get(f"/api/v1/inventory{path}")
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "spoolman_unreachable"

    @pytest.mark.asyncio
    async def test_the_alert_tick_sends_nothing_and_stamps_nothing(
        self, db_session, spoolman_mode, spoolman, monkeypatch
    ):
        db_session.add(
            FilamentSkuSettings(
                material="PLA",
                subtype="Matte",
                brand="Sunlu",
                color_name="Matte",
                stock_break_notified_at=datetime(2026, 8, 1),
            )
        )
        await db_session.commit()
        spoolman.down = True
        breakage, reorder = AsyncMock(), AsyncMock()
        monkeypatch.setattr("backend.app.services.stock_forecast_alerts.async_session", _ctx(db_session))
        with (
            patch("backend.app.services.notification_service.notification_service.on_stock_break_alert", breakage),
            patch("backend.app.services.notification_service.notification_service.on_stock_reorder_alert", reorder),
        ):
            await StockForecastAlerts().tick()

        assert (breakage.await_count, reorder.await_count) == (0, 0)
        row = (await db_session.execute(select(FilamentSkuSettings))).scalar_one()
        assert row.stock_break_notified_at == datetime(2026, 8, 1), "an unanswered Spoolman is not a recovery"


class TestStockAlertsInSpoolmanMode:
    @pytest.mark.asyncio
    async def test_a_spoolman_sku_about_to_run_out_is_announced(self, db_session, spoolman_mode, spoolman):
        db_session.add(Settings(key="forecast_global_lead_time_days", value="7"))
        await db_session.commit()
        # 300 g left at 100 g/day against a 7-day lead time.
        spool = _sm_spool(1, used=700.0)
        spoolman.spools = [spool]
        for days_ago in (5, 4, 3, 2, 1):
            await _sm_usage(db_session, spool, days_ago, 100.0)

        alerts = await find_stock_alerts(db_session, NOW)
        assert [(a.kind, a.material, a.brand, a.days_left) for a in alerts] == [("break", "PLA", "Sunlu", 3)]

    @pytest.mark.asyncio
    async def test_the_tick_announces_it_through_the_usual_event(
        self, db_session, spoolman_mode, spoolman, monkeypatch
    ):
        anchor = datetime.now(timezone.utc)
        db_session.add(Settings(key="forecast_global_lead_time_days", value="7"))
        await db_session.commit()
        spool = _sm_spool(1, used=700.0)
        spoolman.spools = [spool]
        token = await history.source_token(db_session)
        for days_ago in (5, 4, 3, 2, 1):
            db_session.add(
                SpoolmanUsageHistory(
                    source_token=token,
                    spoolman_spool_id=1,
                    spool_registered=REGISTERED,
                    weight_used=100.0,
                    percent_used=0,
                    status="completed",
                    created_at=(anchor - timedelta(days=days_ago)).replace(tzinfo=None),
                )
            )
        await db_session.commit()

        breakage, reorder = AsyncMock(), AsyncMock()
        monkeypatch.setattr("backend.app.services.stock_forecast_alerts.async_session", _ctx(db_session))
        with (
            patch("backend.app.services.notification_service.notification_service.on_stock_break_alert", breakage),
            patch("backend.app.services.notification_service.notification_service.on_stock_reorder_alert", reorder),
        ):
            await StockForecastAlerts().tick()

        assert (breakage.await_count, reorder.await_count) == (1, 0)
        assert breakage.await_args.kwargs["material"] == "PLA"


def _ctx(session):
    """``async_session()`` stand-in handing the test's own session to the tick."""

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_exc):
            return False

    return lambda: _Ctx()
