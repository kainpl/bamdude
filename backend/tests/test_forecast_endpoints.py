"""The forecast endpoints' contract (Task 3 — rows, chart, logistics, CSV).

Written RED before the routes exist. Behavioral sources, quoted per family:

* Sort keys — ``ForecastPanel.tsx`` ``type SortKey`` (:51) is exactly
  ``'material' | 'spools' | 'used' | 'days_left' | 'stock' | 'empty_by' |
  'reorder_by'``; the comparator (:361-399) reads, per key: the lowercased
  composite ``[material, subtype ?? '', brand ?? ''].join(' ')``; totals;
  ``daysRemaining ?? 999999`` (the sentinel is direction-BLIND — rate-less rows
  lead a descending days_left sort, a quirk ported deliberately); the two date
  keys use ``±Infinity`` sentinels flipped WITH the direction so dateless rows
  sink to the end both ways. Default sort = ``material`` + ``asc``
  (:229-234, the ``loadSort(...) ?? 'material' / ?? 'asc'`` initializers).
  The stable 4-part SKU-key tiebreak is a server addition (the client had
  none) fixed by the plan for page-walk stability.
* Chart — top-5 by ``total_used_g`` among rows with a rate (:403-409);
  projection ports ``buildProjectionSeries`` (:196-214: Math.round, clamp at
  0, stop after the first zero). The USAGE series is a NEW capability (spec
  §2.2 as corrected after the T2 review — the shipped chart draws projection
  only), so this file is ``usage_day_series``' FIRST contract and exercises
  the function directly, reset-spool inclusion and all.
* Logistics — ``CartLogisticsRow`` (:1652-1706): the two-point vertical step
  at ``d == lt``, ``stockAtArrival``/``peakG``, ``clampedMax = min(lt +
  ceil(peak/rate) + 5, 365)``, break = ``floor(remaining/rate) < lt``.
* CSV — ``downloadCsv`` (:1426-1455): ten columns, every cell quoted, the
  ``avgSpoolG`` 1000-gram fallback, ``lt || ''``, restock only when lt > 0.
* Permissions — the exact ``RequireAnyPermission(INVENTORY_READ,
  INVENTORY_FORECAST_READ)`` pair ``GET /sku-settings`` carries; the
  regression is a role holding ONLY ``inventory:forecast_read`` (the shipped
  panel 403'd such roles on its heavy feeds — the defect this rewrite kills).
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from backend.app.models.filament_sku_settings import FilamentSkuSettings
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory

pytestmark = pytest.mark.asyncio

# Seed-time clock. The routes compute with the REQUEST-time now, seconds later —
# every exact-value assertion below is drift-proof (single-observation history
# rates don't depend on decay weights; day buckets are written, not derived),
# and the few date assertions accept both sides of a midnight crossing.
NOW = datetime.now(timezone.utc)


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None)


def _acceptable_dates(offset_days: int) -> set[str]:
    """ISO dates ``offset_days`` from today — seed-time AND assert-time, so a
    UTC-midnight crossing mid-test cannot flake an exact-date assertion."""
    return {
        (NOW.date() + timedelta(days=offset_days)).isoformat(),
        (datetime.now(timezone.utc).date() + timedelta(days=offset_days)).isoformat(),
    }


async def _spool(db, **kwargs) -> Spool:
    defaults = {
        "material": "PLA",
        "subtype": None,
        "brand": "Bambu",
        "color_name": "Black",
        "label_weight": 1000,
        "weight_used": 0.0,
        "weight_used_baseline": 0.0,
        "created_at": _naive(NOW - timedelta(days=60)),
    }
    defaults.update(kwargs)
    spool = Spool(**defaults)
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    return spool


async def _usage(db, spool_id: int, days_ago: float, grams: float) -> None:
    db.add(
        SpoolUsageHistory(
            spool_id=spool_id,
            weight_used=grams,
            created_at=_naive(NOW - timedelta(days=days_ago)),
        )
    )
    await db.commit()


async def _rate_events(db, spool_id: int, rate: float) -> None:
    """Two adjacent-day events → a single inter-day observation, so the
    history-tier rate is EXACTLY ``rate`` (grams of the later day / 1-day gap;
    a lone observation's weighted mean is weight-independent, hence immune to
    the seed-vs-request clock drift). std_dev = 0."""
    await _usage(db, spool_id, 3.0, 5.0)
    await _usage(db, spool_id, 2.0, rate)


async def _sku_settings(db, **kwargs) -> FilamentSkuSettings:
    defaults = {
        "material": "PLA",
        "subtype": None,
        "brand": None,
        "color_name": None,
        "lead_time_days": 0,
        "safety_margin_value": 14,
        "safety_margin_unit": "days",
        "alerts_snoozed": False,
    }
    defaults.update(kwargs)
    row = FilamentSkuSettings(**defaults)
    db.add(row)
    await db.commit()
    return row


async def _set_global_lead_time(db, days: int) -> None:
    db.add(Settings(key="forecast_global_lead_time_days", value=str(days)))
    await db.commit()


async def _limited_token(username: str, permissions: list[str]) -> str:
    """A user holding exactly ``permissions`` via a bespoke group (the
    test_zigbee_sensors_api idiom); [] mints a user with no groups at all."""
    from sqlalchemy import text

    from backend.app.core.auth import create_access_token, get_password_hash
    from backend.app.core.database import async_session
    from backend.app.models.group import Group
    from backend.app.models.user import User

    async with async_session() as db:
        user = User(username=username, password_hash=get_password_hash("TestPass123!"), role="user")
        db.add(user)
        if permissions:
            group = Group(name=f"{username}-group", permissions=permissions)
            db.add(group)
        await db.commit()
        await db.refresh(user)
        if permissions:
            await db.refresh(group)
            await db.execute(
                text("INSERT INTO user_groups (user_id, group_id) VALUES (:uid, :gid)"),
                {"uid": user.id, "gid": group.id},
            )
            await db.commit()

    return create_access_token(data={"sub": username})


# ── The 5-SKU sort farm ───────────────────────────────────────────────────────
#
# Engineered so every sort key produces its own order (identified by material —
# one per SKU), pairwise distinct across keys except days_left vs empty_by asc,
# which the DESC direction separates via the rate-less row E:
#
#   SKU  material brand  live  label   used   rate  stock  days  d.u.ROP
#   A    ABS      Zeta    2    2×1000   500     30   1500    50     36
#   B    PETG     Alpha   4    4×1000   100    100   3900    39     25
#   C    PLA      Beta    1    1×4000   300     50   3700    74      0   ← reorder alert (sku lead 60)
#   D    TPU      Gamma   3    3×1000    50    100   2950    29     15
#   E    WOOD     None    1    1×1000     0   none   1000  none   none   ← the nullable row
#
# Rates are exact (single-observation history tier, see _rate_events); with the
# global lead time 0 and default margins (14 days), safety = rate·14, ROP =
# rate·eff_lead + safety, so every derived number above is exact arithmetic.


async def _seed_sort_farm(db) -> None:
    a1 = await _spool(db, material="ABS", brand="Zeta", color_name="Red", weight_used=300.0)
    await _spool(db, material="ABS", brand="Zeta", color_name="Red", weight_used=200.0)
    await _rate_events(db, a1.id, 30.0)

    b1 = await _spool(db, material="PETG", brand="Alpha", color_name="Blue", weight_used=100.0)
    for _ in range(3):
        await _spool(db, material="PETG", brand="Alpha", color_name="Blue")
    await _rate_events(db, b1.id, 100.0)

    c1 = await _spool(db, material="PLA", brand="Beta", color_name="Green", label_weight=4000, weight_used=300.0)
    await _rate_events(db, c1.id, 50.0)
    await _sku_settings(db, material="PLA", brand="Beta", color_name="Green", lead_time_days=60)

    d1 = await _spool(db, material="TPU", brand="Gamma", color_name="Black", weight_used=50.0)
    for _ in range(2):
        await _spool(db, material="TPU", brand="Gamma", color_name="Black")
    await _rate_events(db, d1.id, 100.0)

    await _spool(db, material="WOOD", brand=None, subtype=None, color_name=None, rgba=None)


def _materials(body: dict) -> list[str]:
    return [item["material"] for item in body["items"]]


# Expected material sequences per sort_by — each derived from the farm table
# above by the CLIENT's comparator semantics, never by running the server.
_SORT_CASES = {
    "material_asc": ["ABS", "PETG", "PLA", "TPU", "WOOD"],
    "material_desc": ["WOOD", "TPU", "PLA", "PETG", "ABS"],
    "spools_asc": ["PLA", "WOOD", "ABS", "TPU", "PETG"],  # 1,1,2,3,4 — C before E by the SKU-key tiebreak
    "spools_desc": ["PETG", "TPU", "ABS", "PLA", "WOOD"],  # tiebreak stays ASCENDING on desc too
    "used_asc": ["WOOD", "TPU", "PETG", "PLA", "ABS"],  # 0,50,100,300,500
    "used_desc": ["ABS", "PLA", "PETG", "TPU", "WOOD"],
    "stock_asc": ["WOOD", "ABS", "TPU", "PLA", "PETG"],  # 1000,1500,2950,3700,3900
    "stock_desc": ["PETG", "PLA", "TPU", "ABS", "WOOD"],
    "days_left_asc": ["TPU", "PETG", "ABS", "PLA", "WOOD"],  # 29,39,50,74,∅→999999
    # The client's 999999 sentinel is direction-blind, so the rate-less row
    # LEADS a descending days_left sort — ported verbatim, not "fixed".
    "days_left_desc": ["WOOD", "PLA", "ABS", "PETG", "TPU"],
    "empty_by_asc": ["TPU", "PETG", "ABS", "PLA", "WOOD"],  # dates track days_left
    # ±Infinity sentinel flips with the direction: dateless sinks LAST both ways.
    "empty_by_desc": ["PLA", "ABS", "PETG", "TPU", "WOOD"],
    "reorder_by_asc": ["PLA", "TPU", "PETG", "ABS", "WOOD"],  # trigger +0,+15,+25,+36,∅
    "reorder_by_desc": ["ABS", "PETG", "TPU", "PLA", "WOOD"],
}


# ── Permissions — the regression this rewrite exists for ─────────────────────


class TestForecastPermissions:
    _URLS = (
        "/api/v1/inventory/forecast",
        "/api/v1/inventory/forecast/chart?days=7",
        "/api/v1/inventory/forecast/logistics",
        "/api/v1/inventory/shopping-list/export.csv",
    )

    async def test_forecast_read_alone_opens_all_four_endpoints(self, async_client: AsyncClient):
        """The shipped panel's defect: the tab was gated on inventory:forecast_read
        while its data feeds demanded inventory:read — a forecast-only role got a
        403'd, empty tab. All four new endpoints must answer that role."""
        token = await _limited_token("forecastonly", ["inventory:forecast_read"])
        for url in self._URLS:
            rsp = await async_client.get(url, headers={"Authorization": f"Bearer {token}"})
            assert rsp.status_code == 200, f"{url}: {rsp.status_code} {rsp.text}"

    async def test_inventory_read_alone_also_passes(self, async_client: AsyncClient):
        token = await _limited_token("inventoryonly", ["inventory:read"])
        for url in self._URLS:
            rsp = await async_client.get(url, headers={"Authorization": f"Bearer {token}"})
            assert rsp.status_code == 200, f"{url}: {rsp.status_code} {rsp.text}"

    async def test_no_permission_is_403_on_all_four(self, async_client: AsyncClient):
        token = await _limited_token("nopermsatall", [])
        for url in self._URLS:
            rsp = await async_client.get(url, headers={"Authorization": f"Bearer {token}"})
            assert rsp.status_code == 403, f"{url}: {rsp.status_code} {rsp.text}"


# ── GET /inventory/forecast — rows, sort, filter, page ───────────────────────


class TestForecastRows:
    async def test_the_default_sort_is_the_clients_material_ascending(self, async_client, db_session):
        """ForecastPanel.tsx:229-234 — loadSort falls back to key 'material',
        dir 'asc'; omitting sort_by must serve exactly that order."""
        await _seed_sort_farm(db_session)
        rsp = await async_client.get("/api/v1/inventory/forecast")
        assert rsp.status_code == 200
        assert _materials(rsp.json()) == _SORT_CASES["material_asc"]

    @pytest.mark.parametrize("sort_by", sorted(_SORT_CASES))
    async def test_every_client_sort_key_orders_both_directions(self, async_client, db_session, sort_by):
        await _seed_sort_farm(db_session)
        rsp = await async_client.get(f"/api/v1/inventory/forecast?sort_by={sort_by}")
        assert rsp.status_code == 200
        assert _materials(rsp.json()) == _SORT_CASES[sort_by], sort_by

    async def test_an_unknown_sort_key_is_a_400_not_a_crash(self, async_client):
        rsp = await async_client.get("/api/v1/inventory/forecast?sort_by=velocity_asc")
        assert rsp.status_code == 400

    async def test_a_bare_direction_less_sort_key_is_a_400(self, async_client):
        rsp = await async_client.get("/api/v1/inventory/forecast?sort_by=material")
        assert rsp.status_code == 400

    async def test_page_two_neither_repeats_nor_skips_across_equal_rows(self, async_client, db_session):
        """Five SKUs identical on the primary sort value (same material/subtype/
        brand composite) — only the 4-part SKU-key tiebreak orders them. A page
        walk must partition them exactly: no row twice, none lost."""
        for color in ("c1", "c2", "c3", "c4", "c5"):
            await _spool(db_session, material="PLA", subtype="Matte", brand="BB", color_name=color)

        seen: list[str] = []
        for page in (1, 2, 3):
            rsp = await async_client.get(f"/api/v1/inventory/forecast?page={page}&per_page=2")
            assert rsp.status_code == 200
            body = rsp.json()
            assert body["meta"]["total"] == 5
            assert body["meta"]["last_page"] == 3
            seen += [item["color_name"] for item in body["items"]]
        assert seen == ["c1", "c2", "c3", "c4", "c5"]

    async def test_material_and_brand_filters_narrow_and_meta_counts_the_filtered_set(self, async_client, db_session):
        await _seed_sort_farm(db_session)

        rsp = await async_client.get("/api/v1/inventory/forecast?material=PLA")
        body = rsp.json()
        assert _materials(body) == ["PLA"]
        assert body["meta"]["total"] == 1

        rsp = await async_client.get("/api/v1/inventory/forecast?brand=Zeta")
        body = rsp.json()
        assert _materials(body) == ["ABS"]
        assert body["meta"]["total"] == 1

        rsp = await async_client.get("/api/v1/inventory/forecast?material=PLA&brand=Zeta")
        assert rsp.json()["meta"]["total"] == 0

    async def test_alerts_only_keeps_unsnoozed_alert_rows(self, async_client, db_session):
        await _seed_sort_farm(db_session)
        rsp = await async_client.get("/api/v1/inventory/forecast?alerts_only=true")
        body = rsp.json()
        assert _materials(body) == ["PLA"]  # C — the seeded reorder alert
        assert body["items"][0]["reorder_alert"] is True
        assert body["meta"]["total"] == 1

    async def test_a_snoozed_alert_row_leaves_alerts_only_and_the_count(self, async_client, db_session):
        """The client's badge counts !snoozed && (break || reorder)
        (ForecastPanel.tsx:401) — snoozing C must empty both."""
        await _seed_sort_farm(db_session)
        from sqlalchemy import update

        await db_session.execute(
            update(FilamentSkuSettings).where(FilamentSkuSettings.material == "PLA").values(alerts_snoozed=True)
        )
        await db_session.commit()

        rsp = await async_client.get("/api/v1/inventory/forecast?alerts_only=true")
        assert rsp.json()["meta"]["total"] == 0
        assert rsp.json()["alert_count"] == 0

    async def test_alert_count_ignores_the_material_filter(self, async_client, db_session):
        """The client computes the badge over ALL forecasts, not the filtered
        view (the `alerts` memo reads `forecasts`, not `sortedForecasts`)."""
        await _seed_sort_farm(db_session)
        rsp = await async_client.get("/api/v1/inventory/forecast?material=ABS")
        body = rsp.json()
        assert body["meta"]["total"] == 1
        assert body["alert_count"] == 1  # C's reorder alert, filtered out of items yet counted

    async def test_all_true_returns_every_row_in_one_page(self, async_client, db_session):
        await _seed_sort_farm(db_session)
        rsp = await async_client.get("/api/v1/inventory/forecast?all=true&per_page=2")
        body = rsp.json()
        assert len(body["items"]) == 5
        assert body["meta"] == {"total": 5, "current_page": 1, "per_page": 5, "last_page": 1}

    async def test_global_lead_time_days_rides_the_envelope(self, async_client, db_session):
        await _set_global_lead_time(db_session, 9)
        rsp = await async_client.get("/api/v1/inventory/forecast")
        assert rsp.json()["global_lead_time_days"] == 9

    async def test_a_row_full_of_nones_serializes_instead_of_500ing(self, async_client, db_session):
        """The T2 handoff: subtype/brand/color_name/rgba/rate and every derived
        field can be None — the Pydantic mirror must pass them through as JSON
        nulls, never explode. Row E is exactly that shape."""
        await _seed_sort_farm(db_session)
        rsp = await async_client.get("/api/v1/inventory/forecast?material=WOOD")
        assert rsp.status_code == 200
        (row,) = rsp.json()["items"]
        assert row["material"] == "WOOD"
        for field in (
            "subtype",
            "brand",
            "color_name",
            "rgba",
            "rate_g_day",
            "std_dev",
            "days_remaining",
            "projected_empty_date",
            "days_until_rop",
            "reorder_trigger_date",
        ):
            assert row[field] is None, field
        assert row["rate_tier"] == "none"
        assert row["reorder_point_g"] == 0.0  # forced 0, never null (client truth)
        assert row["safety_stock_g"] == pytest.approx(70.0)  # 5 g/day placeholder × 14-day margin
        assert row["stock_break_alert"] is False and row["reorder_alert"] is False

    async def test_the_row_payload_carries_the_finished_numbers(self, async_client, db_session):
        """Field-by-field pin of one computed row (A) — the endpoint serves the
        engine's finished numbers, not re-derived ones."""
        await _seed_sort_farm(db_session)
        rsp = await async_client.get("/api/v1/inventory/forecast?material=ABS")
        (row,) = rsp.json()["items"]
        assert row["brand"] == "Zeta"
        assert row["color_name"] == "Red"
        assert row["total_spools"] == 2
        assert row["total_remaining_g"] == pytest.approx(1500.0)
        assert row["total_label_g"] == pytest.approx(2000.0)
        assert row["total_used_g"] == pytest.approx(500.0)
        assert row["rate_g_day"] == pytest.approx(30.0)
        assert row["rate_tier"] == "history"
        assert row["eff_lead_time_days"] == 0
        assert row["safety_stock_g"] == pytest.approx(420.0)  # 30 × 14-day margin, lead 0
        assert row["reorder_point_g"] == pytest.approx(420.0)
        assert row["days_remaining"] == 50
        assert row["days_until_rop"] == 36
        assert row["projected_empty_date"] in _acceptable_dates(50)
        assert row["reorder_trigger_date"] in _acceptable_dates(36)
        assert sorted(row["spool_ids"]) == row["spool_ids"] and len(row["spool_ids"]) == 2


# ── GET /inventory/forecast/chart ────────────────────────────────────────────


async def _seed_chart_farm(db) -> None:
    """Six rated SKUs (used 600..100 → top-5 = M1..M5) plus M7: the biggest
    consumer of all with NO rate (younger than a day → the delta tier refuses),
    which the client's `dailyRateG !== null` filter drops BEFORE ranking."""
    m1 = await _spool(db, material="M1", brand="X", color_name="C", weight_used=600.0, rgba="AABBCCFF")
    m1r = await _spool(db, material="M1", brand="X", color_name="C", weight_used=510.0, weight_used_baseline=500.0)
    await _usage(db, m1.id, 40.0, 5.0)  # inside the 90-day rate window, OUTSIDE days=30
    await _usage(db, m1.id, 2.0, 40.0)
    await _usage(db, m1r.id, 2.0, 10.0)  # the reset spool's burn — usage series must count it
    await _usage(db, m1.id, 1.0, 20.0)

    for material, used in (("M2", 500.0), ("M3", 400.0), ("M4", 300.0), ("M6", 100.0)):
        await _spool(db, material=material, brand="X", color_name="C", weight_used=used)  # delta tier

    m5 = await _spool(db, material="M5", brand="X", color_name="C", label_weight=300, weight_used=200.0)
    await _rate_events(db, m5.id, 50.0)  # remaining 100 at 50 g/day → projection dies at day 2

    await _spool(
        db,
        material="M7",
        brand="X",
        color_name="C",
        weight_used=999.0,
        created_at=_naive(NOW - timedelta(hours=1)),
    )


class TestForecastChart:
    async def test_an_unsupported_timeframe_is_a_400(self, async_client):
        rsp = await async_client.get("/api/v1/inventory/forecast/chart?days=14")
        assert rsp.status_code == 400

    @pytest.mark.parametrize("days", (7, 30, 180))
    async def test_the_three_client_timeframes_answer(self, async_client, days):
        rsp = await async_client.get(f"/api/v1/inventory/forecast/chart?days={days}")
        assert rsp.status_code == 200
        assert rsp.json() == {"series": []}

    async def test_top_five_by_used_grams_and_a_rateless_sku_never_ranks(self, async_client, db_session):
        await _seed_chart_farm(db_session)
        rsp = await async_client.get("/api/v1/inventory/forecast/chart?days=30")
        series = rsp.json()["series"]
        assert [s["sku"]["material"] for s in series] == ["M1", "M2", "M3", "M4", "M5"]
        assert series[0]["rgba"] == "AABBCCFF"

    async def test_the_usage_series_is_day_bucketed_reset_included_window_bounded(self, async_client, db_session):
        """M1 through the endpoint: the day-40 event is outside days=30; the
        day-2 bucket sums the healthy spool AND the reset spool (40+10) —
        the series records what was burned, not the rate model's input."""
        await _seed_chart_farm(db_session)
        rsp = await async_client.get("/api/v1/inventory/forecast/chart?days=30")
        m1 = next(s for s in rsp.json()["series"] if s["sku"]["material"] == "M1")
        assert [grams for _, grams in m1["usage"]] == [50.0, 20.0]
        d2, d1 = (entry[0] for entry in m1["usage"])
        assert d2 in _acceptable_dates(-2)
        assert d1 in _acceptable_dates(-1)

    async def test_the_projection_depletes_clamps_at_zero_and_stops(self, async_client, db_session):
        """buildProjectionSeries verbatim: stock = max(0, remaining − rate·d),
        Math.round for display, and the loop breaks AFTER pushing the zero."""
        await _seed_chart_farm(db_session)
        rsp = await async_client.get("/api/v1/inventory/forecast/chart?days=7")
        m5 = next(s for s in rsp.json()["series"] if s["sku"]["material"] == "M5")
        assert [grams for _, grams in m5["projection"]] == [100, 50, 0]
        assert m5["projection"][0][0] in _acceptable_dates(0)
        assert m5["projection"][2][0] in _acceptable_dates(2)

    async def test_a_full_horizon_projection_has_days_plus_one_points(self, async_client, db_session):
        await _seed_chart_farm(db_session)
        rsp = await async_client.get("/api/v1/inventory/forecast/chart?days=7")
        m1 = next(s for s in rsp.json()["series"] if s["sku"]["material"] == "M1")
        assert len(m1["projection"]) == 8  # d0..d7 — nowhere near empty in a week

    async def test_the_series_carries_the_rop_reference(self, async_client, db_session):
        await _seed_chart_farm(db_session)
        rsp = await async_client.get("/api/v1/inventory/forecast/chart?days=7")
        m5 = next(s for s in rsp.json()["series"] if s["sku"]["material"] == "M5")
        assert m5["rop_g"] == pytest.approx(700.0)  # rate 50 × 14-day margin, lead 0


class TestUsageDaySeriesDirectly:
    """``usage_day_series`` shipped in Task 2 with NO contract of its own (the
    T2 review: these chart tests are its first) — so the semantics are pinned
    at the function, not only through the route."""

    async def test_reset_spools_are_included_and_days_sum_across_spools(self, db_session):
        from backend.app.services import forecast_engine

        healthy = await _spool(db_session, material="PLA", brand="B", color_name="Red")
        reset = await _spool(
            db_session, material="PLA", brand="B", color_name="Red", weight_used=510.0, weight_used_baseline=500.0
        )
        await _usage(db_session, healthy.id, 2.0, 40.0)
        await _usage(db_session, reset.id, 2.0, 10.0)

        key = ("PLA", None, "B", "Red")
        series = await forecast_engine.usage_day_series(db_session, sku_keys=[key], days=30, now=NOW)
        assert series[key] == [((NOW - timedelta(days=2)).date(), 50.0)]

    async def test_the_window_is_the_requested_days_not_ninety(self, db_session):
        from backend.app.services import forecast_engine

        spool = await _spool(db_session, material="PLA", brand="B", color_name="Red")
        await _usage(db_session, spool.id, 40.0, 5.0)
        await _usage(db_session, spool.id, 2.0, 20.0)

        key = ("PLA", None, "B", "Red")
        month = await forecast_engine.usage_day_series(db_session, sku_keys=[key], days=30, now=NOW)
        assert [grams for _, grams in month[key]] == [20.0]
        half_year = await forecast_engine.usage_day_series(db_session, sku_keys=[key], days=180, now=NOW)
        assert [grams for _, grams in half_year[key]] == [5.0, 20.0]

    async def test_keys_match_collapsed_but_the_result_keys_are_the_callers(self, db_session):
        """A spool whose brand is "" must answer a caller asking with None —
        and the answer sits under the exact tuple the caller passed."""
        from backend.app.services import forecast_engine

        spool = await _spool(db_session, material="PLA", brand="", color_name="Red")
        await _usage(db_session, spool.id, 2.0, 15.0)

        callers_key = ("PLA", None, None, "Red")
        series = await forecast_engine.usage_day_series(db_session, sku_keys=[callers_key], days=30, now=NOW)
        assert list(series) == [callers_key]
        assert [grams for _, grams in series[callers_key]] == [15.0]

    async def test_an_unknown_sku_gets_an_empty_series_not_a_missing_key(self, db_session):
        from backend.app.services import forecast_engine

        key = ("NOPE", None, None, None)
        series = await forecast_engine.usage_day_series(db_session, sku_keys=[key], days=30, now=NOW)
        assert series == {key: []}


# ── GET /inventory/forecast/logistics ────────────────────────────────────────


async def _add_cart_item(async_client, **payload) -> int:
    defaults = {"material": "PLA", "subtype": None, "brand": None, "color_name": None, "quantity_spools": 1}
    defaults.update(payload)
    rsp = await async_client.post("/api/v1/inventory/shopping-list", json=defaults)
    assert rsp.status_code == 200
    return rsp.json()["id"]


class TestForecastLogistics:
    async def test_the_arrival_bump_is_a_two_point_step_at_the_lead_time(self, async_client, db_session):
        """CartLogisticsRow verbatim: remaining 400 at 50 g/day, lead 10, two
        spools of 1000 g ordered → stockAtArrival 0, peak 2000, the day-10 date
        appears twice (pre-bump 0, post-bump 2000), clampedMax = 10 +
        ceil(2000/50) + 5 = 55 → 57 points with the doubled day."""
        spool = await _spool(db_session, material="PLA", brand="LG", color_name="Red", weight_used=600.0)
        await _rate_events(db_session, spool.id, 50.0)
        await _sku_settings(db_session, material="PLA", brand="LG", color_name="Red", lead_time_days=10)
        item_id = await _add_cart_item(async_client, material="PLA", brand="LG", color_name="Red", quantity_spools=2)

        # Layer localizer (T3 review Minor 2): every number below is pure
        # arithmetic over rate 50 — pin the rate through the ROWS endpoint
        # first, so if the once-observed harness visibility race ever fires
        # again the failure names the engine/visibility layer, never the
        # logistics arithmetic.
        rows_rsp = await async_client.get("/api/v1/inventory/forecast?material=PLA")
        assert [r["rate_g_day"] for r in rows_rsp.json()["items"]] == [pytest.approx(50.0)]

        rsp = await async_client.get("/api/v1/inventory/forecast/logistics")
        assert rsp.status_code == 200
        row = next(r for r in rsp.json() if r["item_id"] == item_id)

        assert row["arrival_day"] == 10
        series = row["series"]
        assert len(series) == 57
        assert series[0][1] == 400
        assert series[10] == [series[11][0], 0]  # same date twice…
        assert series[11][1] == 2000  # …stepping UP by the ordered grams
        assert series[10][0] in _acceptable_dates(10)
        assert series[12][1] == 1950  # post-arrival depletion resumes
        # floor(400/50) = 8 < lead 10 → the stock breaks before the parcel lands
        assert row["stock_break_before_arrival"] is True
        # The banner's headline number (T3 review Important 1): stockBreaksAt =
        # Math.floor(remaining / rate) — NOT the series' first zero, which
        # rounding puts a day later in general (rem 420 @ 50: the series
        # zeroes at d9 while the client banner says 8).
        assert row["stock_break_day"] == 8
        # In the client the flag IS the day's non-nullness (hasBreak =
        # stockBreaksAt !== null, ForecastPanel.tsx:1708) — pin the iff.
        assert (row["stock_break_day"] is not None) == row["stock_break_before_arrival"]
        # rate 50, lead 10, σ=0: safety = 50·14 = 700; ROP = 50·10 + 700 = 1200
        assert row["safety_stock_g"] == pytest.approx(700.0)
        assert row["rop_g"] == pytest.approx(1200.0)

    async def test_a_covered_sku_reports_no_break(self, async_client, db_session):
        spool = await _spool(
            db_session, material="PETG", brand="LG", color_name="Blue", label_weight=3000, weight_used=1000.0
        )
        await _rate_events(db_session, spool.id, 10.0)
        await _sku_settings(db_session, material="PETG", brand="LG", color_name="Blue", lead_time_days=5)
        item_id = await _add_cart_item(async_client, material="PETG", brand="LG", color_name="Blue", quantity_spools=1)

        rsp = await async_client.get("/api/v1/inventory/forecast/logistics")
        row = next(r for r in rsp.json() if r["item_id"] == item_id)
        assert row["stock_break_before_arrival"] is False
        # floor(2000/10) = 200 ≥ lead 5 → the client memo returns null: no day
        assert row["stock_break_day"] is None
        assert (row["stock_break_day"] is not None) == row["stock_break_before_arrival"]
        assert row["arrival_day"] == 5
        assert row["series"][5][1] == 1950  # 2000 − 10·5
        assert row["series"][6][1] == 4950  # + one 3000 g spool

    async def test_the_arrival_bump_is_sized_by_the_SERVED_spool_mean(self, async_client, db_session):
        """Final review F5. The live totals cannot size a spool of an
        archived-only SKU — ``total_spools`` is 0 there by design — so the
        bump used to be priced at the fabricated 1000 g while the Add-to-cart
        dialog that placed the very same order divided by the row's real
        ``avg_spool_label_g``. One rule, every consumer."""
        spool = await _spool(
            db_session,
            material="PLA",
            brand="AR",
            color_name="Gold",
            label_weight=500,
            weight_used=500.0,
            archived_at=_naive(NOW - timedelta(days=2)),
        )
        await _rate_events(db_session, spool.id, 50.0)
        item_id = await _add_cart_item(async_client, material="PLA", brand="AR", color_name="Gold", quantity_spools=2)

        rows_rsp = await async_client.get("/api/v1/inventory/forecast?brand=AR")
        (r,) = rows_rsp.json()["items"]
        assert r["total_spools"] == 0, "live-gated: this SKU has no live stock"
        assert r["avg_spool_label_g"] == pytest.approx(500.0)

        rsp = await async_client.get("/api/v1/inventory/forecast/logistics")
        row = next(x for x in rsp.json() if x["item_id"] == item_id)
        assert row["arrival_day"] == 0
        # 2 spools × the REAL 500 g = 1000 — the fabricated mean would say 2000.
        assert row["series"][0][1] == 0
        assert row["series"][1][1] == 1000

    async def test_an_item_with_no_rate_gets_a_null_series_not_a_crash(self, async_client):
        """The client renders 'no usage data' for a cart item whose SKU has no
        forecast or no rate — the server signals that with series: null."""
        item_id = await _add_cart_item(async_client, material="NOPE", quantity_spools=1)
        rsp = await async_client.get("/api/v1/inventory/forecast/logistics")
        row = next(r for r in rsp.json() if r["item_id"] == item_id)
        assert row["series"] is None
        assert row["arrival_day"] is None
        assert row["rop_g"] is None and row["safety_stock_g"] is None
        assert row["stock_break_before_arrival"] is False
        assert row["stock_break_day"] is None


# ── GET /inventory/shopping-list/export.csv ──────────────────────────────────

_CSV_HEADERS = [
    "Qty",
    "Material",
    "Brand",
    "Subtype",
    "Color",
    "Weight (g)",
    "Lead Time (d)",
    "Expected Restock",
    "Status",
    "Note",
]


def _csv_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


class TestShoppingListCsv:
    async def test_the_header_row_and_the_attachment_headers(self, async_client):
        rsp = await async_client.get("/api/v1/inventory/shopping-list/export.csv")
        assert rsp.status_code == 200
        assert rsp.headers["content-type"].startswith("text/csv")
        assert rsp.headers["content-disposition"] == 'attachment; filename="shopping-list.csv"'
        assert _csv_rows(rsp.text) == [_CSV_HEADERS]

    async def test_commas_and_quotes_in_a_value_round_trip(self, async_client, db_session):
        await _set_global_lead_time(db_session, 4)
        await _add_cart_item(
            async_client,
            material='PLA, "Tough"',
            note='say "hi", twice',
            quantity_spools=2,
        )
        rsp = await async_client.get("/api/v1/inventory/shopping-list/export.csv")
        rows = _csv_rows(rsp.text)
        assert rows[0] == _CSV_HEADERS
        (row,) = rows[1:]
        assert row[0] == "2"
        assert row[1] == 'PLA, "Tough"'
        assert row[9] == 'say "hi", twice'
        # No matching SKU → the client's 1000 g average-spool fallback
        assert row[5] == "2000"
        # Unmatched SKU → global lead time; restock = today + 4, ISO
        assert row[6] == "4"
        assert row[7] in _acceptable_dates(4)
        assert row[8] == "pending"

    async def test_a_matched_sku_prices_the_weight_and_lead_time_from_its_row(self, async_client, db_session):
        """avgSpoolG = the row's served ``avg_spool_label_g``; the lead time is
        the SKU's effective one, not the global. (Both spools here are live, so
        the archived-inclusive mean and the live one coincide — the case where
        they diverge is the next test.)"""
        await _spool(db_session, material="PETG", brand="CS", color_name="Teal")
        await _spool(db_session, material="PETG", brand="CS", color_name="Teal", label_weight=500)
        await _sku_settings(db_session, material="PETG", brand="CS", color_name="Teal", lead_time_days=7)
        await _add_cart_item(async_client, material="PETG", brand="CS", color_name="Teal", quantity_spools=3)

        rsp = await async_client.get("/api/v1/inventory/shopping-list/export.csv")
        (row,) = _csv_rows(rsp.text)[1:]
        assert row[1] == "PETG"
        assert row[2] == "CS"
        assert row[4] == "Teal"
        assert row[5] == "2250"  # 3 × (1500 / 2)
        assert row[6] == "7"
        assert row[7] in _acceptable_dates(7)

    async def test_the_weight_column_uses_the_served_mean_for_an_archived_only_sku(self, async_client, db_session):
        """Final review F5. An archived-only SKU serves ``total_spools`` 0, so
        the live divisor collapsed to the 1000 g guess and the export priced
        the order at double its real weight — while the dialog that placed it
        used the row's archived-inclusive mean."""
        await _spool(
            db_session,
            material="TPU",
            brand="AR",
            color_name="Gold",
            label_weight=500,
            weight_used=500.0,
            archived_at=_naive(NOW - timedelta(days=2)),
        )
        await _add_cart_item(async_client, material="TPU", brand="AR", color_name="Gold", quantity_spools=3)

        rsp = await async_client.get("/api/v1/inventory/shopping-list/export.csv")
        (row,) = _csv_rows(rsp.text)[1:]
        assert row[1] == "TPU"
        assert row[5] == "1500", "3 × the real 500 g — the fabricated mean would say 3000"

    async def test_a_zero_lead_time_leaves_both_cells_empty(self, async_client, db_session):
        """The client writes `lt || ''` and skips the restock date at lt 0."""
        await _add_cart_item(async_client, material="ASA", quantity_spools=1)
        rsp = await async_client.get("/api/v1/inventory/shopping-list/export.csv")
        (row,) = _csv_rows(rsp.text)[1:]
        assert row[6] == ""
        assert row[7] == ""
