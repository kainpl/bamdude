"""The forecast engine's contract, written before the engine exists (Task 1 — RED).

This suite DEFINES ``backend/app/services/forecast_engine.py`` (Task 2 conforms
to these tests, not vice versa):

* ``history_rate(day_buckets: list[tuple[date, float]], now) -> RateEstimate | None``
  — pure; takes UTC-day buckets (the SQL layer's output), naive-UTC-compatible.
* ``delta_rate(total_used_g: float, oldest_created_at: datetime | None, now) -> float | None``
  — pure over the aggregates SQL delivers; the caller feeds it the baseline-aware
  clamped total ``Σ max(0, weight_used - weight_used_baseline)`` over ALL spools
  of the SKU (archived included — the client passes ``group.allSpools``) and the
  oldest ``created_at`` (naive UTC, as the DB stores it).
* ``finish_row(*, rate, std_dev, eff_lead_time_days, margin_value, margin_unit,
  total_remaining_g, now, snoozed)`` — the client-only arithmetic (Z·σ·√L, ROP,
  dates, alert booleans); returns an object exposing the finishing fields.
* ``@dataclass SkuForecastRow`` + ``async def compute_forecast(db, *, now=None)``.

Sources of truth, in order:
1. Rate math: ``backend/tests/forecast_vectors/rate_vectors.json`` — frozen by
   EXECUTING the shipped ``stock_forecast_alerts.history_rate/delta_rate``. Never
   hand-derived. If a vector fails, the engine is wrong, never the vector.
2. Finishing math: ``frontend/src/components/ForecastPanel.tsx`` — every constant
   below was verified against the component before being trusted.

── Client-truth ledger (verified in ForecastPanel.tsx; where spec/plan/server
   disagree, the client wins — recorded per the operator's ruling) ─────────────
* Z = 1.65; σ = std_dev if present else rate·0.2 (else 0); statistical part is
  Z·σ·√eff_lead_time; days counts use floor(); both alert boundaries inclusive.
* ``kg`` margin unit EXISTS (``marginGrams``: value·1000). The API stores it
  (``Literal["days", "g", "kg"]`` in routes/inventory.py) but the spec omits it
  and today's alert service would misread it as days (its ``else`` branch).
* A ``days`` margin with NO rate uses a 5 g/day placeholder (value·5) — but a
  rate of exactly 0.0 is NOT null: margin becomes 0·value = 0, no placeholder.
* ``reorderPointG`` is forced to 0 when rate is null (safety stock still real).
* Stock break additionally requires ``eff_lead_time_days > 0`` (spec §2.1 omits
  the gate; client and alert service both have it).
* Reorder is mutually exclusive with stock break (break wins outright).
* ``reorder_trigger_date`` clamps at today (``max(0, daysUntilROP)``) while
  ``days_until_rop`` itself stays negative.
* Delta tier keeps ``std_dev`` None on the row (σ=rate·0.2 lives only inside the
  safety-stock formula); rate 0 or None → no dates, no alerts, no ZeroDivision.

Every test but ``TestTheVectorsThemselves`` consumes the ``engine`` fixture and
is RED with ``ModuleNotFoundError`` until Task 2 — per test, not as one
collection error that would mask the matrix.
"""

from __future__ import annotations

import dataclasses
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from backend.app.models.filament_sku_settings import FilamentSkuSettings
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory

VECTORS_PATH = Path(__file__).parent / "forecast_vectors" / "rate_vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))

# The vectors' reference clock — deliberately a UTC midnight (see the generator).
NOW = datetime.fromisoformat(VECTORS["now"])
TODAY = NOW.date()


@pytest.fixture
def engine():
    """The module under test. It does not exist yet — Task 2 writes it — so each
    consumer of this fixture fails individually with ModuleNotFoundError."""
    import backend.app.services.forecast_engine as forecast_engine

    return forecast_engine


# ── Vector plumbing ───────────────────────────────────────────────────────────


def _history_vector(name: str) -> dict:
    return next(v for v in VECTORS["history"] if v["name"] == name)


def _delta_vector(name: str) -> dict:
    return next(v for v in VECTORS["delta"] if v["name"] == name)


def _buckets(records: list[dict]) -> list[tuple[date, float]]:
    """Day-bucket a vector's records the way the SQL layer will: UTC calendar
    day of ``created_at``, grams summed per day, one tuple per distinct day."""
    by_day: dict[date, float] = {}
    for r in records:
        day = date.fromisoformat(r["created_at"][:10])
        by_day[day] = by_day.get(day, 0.0) + r["weight_used"]
    return sorted(by_day.items())


def _delta_inputs(spools: list[dict]) -> tuple[float, datetime | None]:
    """The aggregates ``delta_rate`` takes: baseline-aware clamped total and the
    oldest ``created_at`` (naive UTC, exactly as the DB hands them over)."""
    total = sum(max(0.0, s["weight_used"] - s["weight_used_baseline"]) for s in spools)
    oldest = min((datetime.fromisoformat(s["created_at"]) for s in spools), default=None)
    return total, oldest


_HISTORY_PARAMS = [pytest.param(v, id=v["name"]) for v in VECTORS["history"]]
_DELTA_PARAMS = [pytest.param(v, id=v["name"]) for v in VECTORS["delta"]]


# ── The truth source itself (GREEN today — the only green in this file) ───────


class TestTheVectorsThemselves:
    """Sanity of the frozen JSON against the SHIPPED implementation. These run
    green before the engine exists and keep the vectors honest forever."""

    def test_the_frozen_vectors_match_the_shipped_implementation(self):
        from backend.tests.forecast_vectors.generate_vectors import build_vectors

        regenerated = json.loads(json.dumps(build_vectors()))
        assert regenerated == VECTORS, "rate_vectors.json is stale — it no longer matches stock_forecast_alerts"

    def test_the_half_life_vector_demonstrates_half_weight_at_thirty_days(self):
        """Property of the frozen truth, not of the engine: the pair holds one
        10 g/day observation exactly 30.0 days old and one 20 g/day observation
        0 days old, so the frozen mean equals (0.5·10 + 1.0·20)/1.5 only if the
        weight at exactly 30 days is exactly 0.5."""
        frozen = _history_vector("half_life_pair_weight_at_thirty_days")["expected"]
        assert frozen["rate"] == pytest.approx((0.5 * 10.0 + 1.0 * 20.0) / 1.5, rel=1e-12)

    def test_a_single_day_is_frozen_as_unmeasurable(self):
        assert _history_vector("single_day_refuses")["expected"] is None
        assert _history_vector("single_record_refuses")["expected"] is None


# ── Golden replay: the engine must reproduce the shipped math exactly ─────────


class TestTheGoldenVectors:
    @pytest.mark.parametrize("vector", _HISTORY_PARAMS)
    def test_history_rate_reproduces_the_frozen_vector(self, engine, vector):
        estimate = engine.history_rate(_buckets(vector["records"]), NOW)
        if vector["expected"] is None:
            assert estimate is None
        else:
            assert estimate is not None
            assert estimate.rate == pytest.approx(vector["expected"]["rate"], rel=1e-9, abs=1e-12)
            assert estimate.std_dev == pytest.approx(vector["expected"]["std_dev"], rel=1e-9, abs=1e-12)

    @pytest.mark.parametrize("vector", _DELTA_PARAMS)
    def test_delta_rate_reproduces_the_frozen_vector(self, engine, vector):
        total_used, oldest = _delta_inputs(vector["spools"])
        rate = engine.delta_rate(total_used, oldest, NOW)
        if vector["expected_rate"] is None:
            assert rate is None
        else:
            assert rate == pytest.approx(vector["expected_rate"], rel=1e-9)

    def test_bucket_order_does_not_matter(self, engine):
        """SQL GROUP BY guarantees no order — the engine must sort, not assume."""
        vector = _history_vector("six_events_across_four_days_mixed_grams")
        estimate = engine.history_rate(list(reversed(_buckets(vector["records"]))), NOW)
        assert estimate is not None
        assert estimate.rate == pytest.approx(vector["expected"]["rate"], rel=1e-9)
        assert estimate.std_dev == pytest.approx(vector["expected"]["std_dev"], rel=1e-9)

    def test_decay_weight_at_exactly_thirty_days_is_half(self, engine):
        """The edge-matrix pin, via the vector pair — never by reaching into the
        engine's internals. The frozen value itself encodes w(30 d) = 0.5 (see
        TestTheVectorsThemselves); the engine must land on the same number."""
        vector = _history_vector("half_life_pair_weight_at_thirty_days")
        estimate = engine.history_rate(_buckets(vector["records"]), NOW)
        assert estimate is not None
        assert estimate.rate == pytest.approx(vector["expected"]["rate"], rel=1e-9)


class TestRateHelperEdges:
    def test_history_refuses_an_empty_bucket_list(self, engine):
        assert engine.history_rate([], NOW) is None

    def test_history_refuses_a_single_day(self, engine):
        assert engine.history_rate([(date(2026, 8, 26), 150.0)], NOW) is None

    def test_delta_refuses_zero_consumption(self, engine):
        assert engine.delta_rate(0.0, datetime(2026, 8, 19), NOW) is None

    def test_delta_needs_a_created_at(self, engine):
        """No spool ever recorded a creation time → nothing anchors the window."""
        assert engine.delta_rate(100.0, None, NOW) is None

    def test_delta_refuses_a_group_younger_than_a_day(self, engine):
        oldest = (NOW - timedelta(hours=3)).replace(tzinfo=None)
        assert engine.delta_rate(50.0, oldest, NOW) is None


# ── finish_row: the client-only arithmetic, hand vectors ──────────────────────


def _finish(engine, **overrides):
    kwargs = {
        "rate": 10.0,
        "std_dev": 4.0,
        "eff_lead_time_days": 9,
        "margin_value": 2,
        "margin_unit": "days",
        "total_remaining_g": 500.0,
        "now": NOW,
        "snoozed": False,
    }
    kwargs.update(overrides)
    return engine.finish_row(**kwargs)


class TestFinishRowHandVectors:
    def test_safety_stock_and_rop_hand_vector(self, engine):
        # rate=10 g/day, history-tier std_dev=4, eff_lead_time=9 days,
        # margin: 2 days => 20 g.  Z95=1.65.  (ForecastPanel.tsx, verified.)
        row = _finish(engine)
        assert row.safety_stock_g == pytest.approx(1.65 * 4.0 * 3.0 + 20.0)  # 39.8
        assert row.reorder_point_g == pytest.approx(10.0 * 9 + 39.8)  # 129.8
        assert row.days_remaining == 50
        assert row.days_until_rop == 37
        assert row.projected_empty_date == TODAY + timedelta(days=50)
        assert row.reorder_trigger_date == TODAY + timedelta(days=37)
        assert row.stock_break_alert is False and row.reorder_alert is False

    def test_sigma_falls_back_to_fifth_of_rate_on_delta_tier(self, engine):
        row = _finish(
            engine,
            rate=10.0,
            std_dev=None,
            eff_lead_time_days=4,
            margin_value=0,
            margin_unit="g",
            total_remaining_g=100.0,
        )
        assert row.safety_stock_g == pytest.approx(1.65 * 2.0 * 2.0)  # σ = rate·0.2
        assert row.reorder_point_g == pytest.approx(10.0 * 4 + 6.6)
        assert row.days_remaining == 10
        assert row.days_until_rop == math.floor((100.0 - 46.6) / 10.0)  # 5

    def test_the_stock_break_boundary_is_inclusive(self, engine):
        # days_remaining == eff_lead_time → stock break IS on (<= in the client).
        at_boundary = _finish(
            engine,
            rate=10.0,
            std_dev=0.0,
            eff_lead_time_days=5,
            margin_value=0,
            margin_unit="g",
            total_remaining_g=50.0,
        )
        assert at_boundary.days_remaining == 5
        assert at_boundary.stock_break_alert is True
        assert at_boundary.reorder_alert is False, "break wins outright — never both"

        just_outside = _finish(
            engine,
            rate=10.0,
            std_dev=0.0,
            eff_lead_time_days=5,
            margin_value=0,
            margin_unit="g",
            total_remaining_g=60.0,
        )
        assert just_outside.days_remaining == 6
        assert just_outside.stock_break_alert is False

    def test_the_reorder_boundary_is_inclusive(self, engine):
        # days_until_rop == 0 → reorder IS on (<= 0 in the client). The 100 g
        # margin keeps the ROP far above rate·lead so the row is NOT in break.
        at_boundary = _finish(
            engine,
            rate=10.0,
            std_dev=0.0,
            eff_lead_time_days=2,
            margin_value=100,
            margin_unit="g",
            total_remaining_g=125.0,
        )
        assert at_boundary.reorder_point_g == pytest.approx(120.0)
        assert at_boundary.days_until_rop == 0
        assert at_boundary.days_remaining == 12  # comfortably beyond the 2-day lead
        assert at_boundary.stock_break_alert is False
        assert at_boundary.reorder_alert is True

        just_outside = _finish(
            engine,
            rate=10.0,
            std_dev=0.0,
            eff_lead_time_days=2,
            margin_value=100,
            margin_unit="g",
            total_remaining_g=130.0,
        )
        assert just_outside.days_until_rop == 1
        assert just_outside.reorder_alert is False

    def test_a_stock_break_is_not_also_a_reorder(self, engine):
        row = _finish(
            engine,
            rate=10.0,
            std_dev=0.0,
            eff_lead_time_days=5,
            margin_value=0,
            margin_unit="g",
            total_remaining_g=10.0,
        )
        assert row.days_remaining == 1
        assert row.days_until_rop == -4
        assert row.stock_break_alert is True
        assert row.reorder_alert is False

    def test_no_lead_time_means_no_stock_break_but_reorder_still_fires(self, engine):
        """Client-verified: the break needs ``effectiveLeadTimeDays > 0`` ("runs
        out before replenishment arrives" is meaningless without a lead time);
        the reorder test carries no such gate."""
        row = _finish(
            engine,
            rate=10.0,
            std_dev=0.0,
            eff_lead_time_days=0,
            margin_value=0,
            margin_unit="g",
            total_remaining_g=0.0,
        )
        assert row.days_remaining == 0
        assert row.stock_break_alert is False
        assert row.days_until_rop == 0
        assert row.reorder_alert is True

    def test_a_margin_in_grams_is_taken_literally(self, engine):
        row = _finish(
            engine,
            rate=10.0,
            std_dev=0.0,
            eff_lead_time_days=1,
            margin_value=50,
            margin_unit="g",
            total_remaining_g=500.0,
        )
        assert row.safety_stock_g == pytest.approx(50.0)
        assert row.reorder_point_g == pytest.approx(60.0)

    def test_a_margin_in_days_converts_through_the_rate(self, engine):
        row = _finish(
            engine,
            rate=10.0,
            std_dev=0.0,
            eff_lead_time_days=1,
            margin_value=3,
            margin_unit="days",
            total_remaining_g=500.0,
        )
        assert row.safety_stock_g == pytest.approx(30.0)
        assert row.reorder_point_g == pytest.approx(40.0)

    def test_a_margin_in_kg_is_a_thousand_grams(self, engine):
        """Client truth the spec omits: ``marginGrams`` maps kg → value·1000, and
        the API stores 'kg' (Literal in routes/inventory.py). Today's alert
        service would misread it as days (its else-branch) — the engine must not."""
        row = _finish(
            engine,
            rate=10.0,
            std_dev=0.0,
            eff_lead_time_days=1,
            margin_value=1,
            margin_unit="kg",
            total_remaining_g=5000.0,
        )
        assert row.safety_stock_g == pytest.approx(1000.0)
        assert row.reorder_point_g == pytest.approx(1010.0)

    def test_no_rate_margin_days_uses_the_five_gram_placeholder(self, engine):
        """Client truth: with rate None a 'days' margin is worth value·5 g (the
        placeholder keeps the safety stock non-zero for brand-new SKUs), the ROP
        is forced to 0, and every day/date field is None with both alerts off."""
        row = _finish(
            engine,
            rate=None,
            std_dev=None,
            eff_lead_time_days=9,
            margin_value=2,
            margin_unit="days",
            total_remaining_g=500.0,
        )
        assert row.safety_stock_g == pytest.approx(10.0)  # 2 days · 5 g placeholder, σ term 0
        assert row.reorder_point_g == pytest.approx(0.0)  # client forces 0 without a rate
        assert row.days_remaining is None
        assert row.projected_empty_date is None
        assert row.days_until_rop is None
        assert row.reorder_trigger_date is None
        assert row.stock_break_alert is False and row.reorder_alert is False

    def test_a_zero_rate_from_flat_history_produces_no_dates(self, engine):
        """Rate 0.0 is measured, not missing (the zero-gram golden vector): the
        'days' margin is 0·value = 0 — NOT the 5 g placeholder, which the client
        applies only to a null rate — and no date math runs (no ZeroDivision)."""
        row = _finish(
            engine,
            rate=0.0,
            std_dev=0.0,
            eff_lead_time_days=9,
            margin_value=2,
            margin_unit="days",
            total_remaining_g=500.0,
        )
        assert row.safety_stock_g == pytest.approx(0.0)
        assert row.reorder_point_g == pytest.approx(0.0)
        assert row.days_remaining is None
        assert row.projected_empty_date is None
        assert row.days_until_rop is None
        assert row.reorder_trigger_date is None
        assert row.stock_break_alert is False and row.reorder_alert is False

    def test_the_reorder_trigger_date_never_lands_in_the_past(self, engine):
        """Client-verified: the date clamps at today (max(0, daysUntilROP)) while
        days_until_rop itself keeps its raw negative value."""
        row = _finish(
            engine,
            rate=10.0,
            std_dev=0.0,
            eff_lead_time_days=2,
            margin_value=100,
            margin_unit="g",
            total_remaining_g=90.0,
        )
        assert row.days_until_rop == -3
        assert row.reorder_trigger_date == TODAY
        assert row.stock_break_alert is False
        assert row.reorder_alert is True

    def test_snooze_keeps_the_numbers_and_the_flags(self, engine):
        """Snooze suppresses nothing inside the row — the flags stay computed and
        ``alerts_snoozed`` rides along; the CONSUMERS (panel alert list, alert
        task, alerts_only filter) are the ones that honour it."""
        row = _finish(
            engine,
            rate=10.0,
            std_dev=0.0,
            eff_lead_time_days=5,
            margin_value=0,
            margin_unit="g",
            total_remaining_g=10.0,
            snoozed=True,
        )
        assert row.stock_break_alert is True
        assert row.alerts_snoozed is True
        assert row.days_remaining == 1


# ── The dataclass contract ────────────────────────────────────────────────────


class TestTheRowContract:
    def test_sku_forecast_row_is_the_agreed_dataclass(self, engine):
        assert dataclasses.is_dataclass(engine.SkuForecastRow)
        names = {f.name for f in dataclasses.fields(engine.SkuForecastRow)}
        assert names == {
            "material",
            "subtype",
            "brand",
            "color_name",
            "rgba",
            "total_spools",
            "total_remaining_g",
            "total_label_g",
            "total_used_g",
            "rate_g_day",
            "rate_tier",
            "std_dev",
            "eff_lead_time_days",
            "safety_stock_g",
            "reorder_point_g",
            "days_remaining",
            "projected_empty_date",
            "days_until_rop",
            "reorder_trigger_date",
            "stock_break_alert",
            "reorder_alert",
            "alerts_snoozed",
            "spool_ids",
        }


# ── compute_forecast: DB seeding helpers ──────────────────────────────────────


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None)


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


async def _seed_vector_usage(db, vector_name: str, spool_map: dict[int, int]) -> None:
    """Insert a golden vector's records as real usage rows, so the DB-level rate
    must land exactly on the frozen value. ``spool_map`` maps the vector's
    spool_id onto the seeded row's real id."""
    for r in _history_vector(vector_name)["records"]:
        db.add(
            SpoolUsageHistory(
                spool_id=spool_map[r["spool_id"]],
                weight_used=r["weight_used"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
        )
    await db.commit()


async def _rows_by_key(engine, db) -> dict[tuple, object]:
    rows = await engine.compute_forecast(db, now=NOW)
    return {(r.material, r.subtype, r.brand, r.color_name): r for r in rows}


_PLA_BLACK = ("PLA", None, "Bambu", "Black")


# ── The edge matrix, compute_forecast level ───────────────────────────────────


class TestComputeForecastTiers:
    async def test_a_sku_with_no_usage_history_falls_to_the_delta_tier(self, engine, db_session):
        await _spool(db_session, weight_used=100.0, created_at=_naive(NOW - timedelta(days=10)))

        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.rate_tier == "delta"
        assert row.rate_g_day == pytest.approx(_delta_vector("one_spool_ten_days")["expected_rate"], rel=1e-9)
        assert row.std_dev is None, "the delta tier has no spread of its own — σ=rate·0.2 lives in the safety stock"

    async def test_a_spool_younger_than_a_day_has_no_rate_and_no_dates(self, engine, db_session):
        await _spool(db_session, weight_used=50.0, created_at=_naive(NOW - timedelta(hours=3)))

        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.rate_tier == "none"
        assert row.rate_g_day is None
        assert row.days_remaining is None
        assert row.projected_empty_date is None
        assert row.days_until_rop is None
        assert row.reorder_trigger_date is None
        assert row.stock_break_alert is False and row.reorder_alert is False

    async def test_reset_spools_stay_out_of_the_history_tier(self, engine, db_session):
        """A reset spool's usage rows have no anchor — if they leaked into the
        buckets, the rate could not land on the frozen vector value."""
        clean = await _spool(db_session, weight_used=200.0)
        await _seed_vector_usage(db_session, "two_days_with_a_five_day_gap", {1: clean.id})
        poisoned = await _spool(db_session, weight_used=900.0, weight_used_baseline=500.0)
        await _usage(db_session, poisoned.id, 3.4, 3000.0)
        await _usage(db_session, poisoned.id, 1.4, 3000.0)

        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.rate_tier == "history"
        frozen = _history_vector("two_days_with_a_five_day_gap")["expected"]
        assert row.rate_g_day == pytest.approx(frozen["rate"], rel=1e-9)
        assert row.std_dev == pytest.approx(frozen["std_dev"], rel=1e-9, abs=1e-12)

    async def test_only_reset_spools_left_still_get_a_baseline_aware_delta(self, engine, db_session):
        """Reset spools are OUT of the history tier but IN the delta tier,
        baseline-aware: 900 used over an 800 baseline in 10 days is 10 g/day."""
        spool = await _spool(
            db_session,
            weight_used=900.0,
            weight_used_baseline=800.0,
            created_at=_naive(NOW - timedelta(days=10)),
        )
        await _usage(db_session, spool.id, 3.0, 400.0)  # pre-reset rows — history must ignore them
        await _usage(db_session, spool.id, 1.0, 400.0)

        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.rate_tier == "delta"
        assert row.rate_g_day == pytest.approx(_delta_vector("baseline_aware_reset")["expected_rate"], rel=1e-9)

    async def test_usage_beyond_ninety_days_is_outside_the_window(self, engine, db_session):
        """The spec'd deviation from the shipped panel: the window is 90 days of
        TIME, not 5000 rows. A 5000 g event at day 91 must change nothing."""
        plain = await _spool(db_session, material="PLA", weight_used=200.0)
        await _seed_vector_usage(db_session, "two_days_with_a_five_day_gap", {1: plain.id})
        with_relic = await _spool(db_session, material="PETG", weight_used=200.0)
        await _seed_vector_usage(db_session, "two_days_with_a_five_day_gap", {1: with_relic.id})
        await _usage(db_session, with_relic.id, 91.0, 5000.0)

        rows = await _rows_by_key(engine, db_session)
        frozen_rate = _history_vector("two_days_with_a_five_day_gap")["expected"]["rate"]
        assert rows[("PLA", None, "Bambu", "Black")].rate_g_day == pytest.approx(frozen_rate, rel=1e-9)
        assert rows[("PETG", None, "Bambu", "Black")].rate_g_day == pytest.approx(frozen_rate, rel=1e-9)

    async def test_usage_at_day_89_still_counts(self, engine, db_session):
        """Inside the window by a day: two old-but-eligible days must still make
        a history tier (the spool's age gives it a delta fallback, so a wrongly
        narrowed window would show up as tier 'delta', not as an error)."""
        spool = await _spool(db_session, weight_used=100.0, created_at=_naive(NOW - timedelta(days=100)))
        await _usage(db_session, spool.id, 89.5, 100.0)
        await _usage(db_session, spool.id, 88.5, 100.0)

        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.rate_tier == "history"
        assert row.rate_g_day is not None

    async def test_utc_day_bucketing_across_midnight(self, engine, db_session):
        """23:59Z and 00:01Z are different UTC days — two buckets, so the history
        tier engages; a local-time or broken bucketing would collapse them into
        one day and fall through to the delta tier."""
        spool = await _spool(db_session, weight_used=200.0, created_at=_naive(NOW - timedelta(days=60)))
        db_session.add(
            SpoolUsageHistory(spool_id=spool.id, weight_used=100.0, created_at=datetime(2026, 8, 25, 23, 59))
        )
        db_session.add(SpoolUsageHistory(spool_id=spool.id, weight_used=100.0, created_at=datetime(2026, 8, 26, 0, 1)))
        await db_session.commit()

        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.rate_tier == "history"


class TestComputeForecastTotals:
    async def test_archived_grams_count_as_consumption_not_stock(self, engine, db_session):
        live = await _spool(db_session, weight_used=200.0)
        await _spool(db_session, weight_used=900.0, archived_at=_naive(NOW - timedelta(days=2)))

        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.total_remaining_g == pytest.approx(800.0), "stock is live spools only"
        assert row.total_used_g == pytest.approx(1100.0), "consumption survives archiving"
        assert row.total_label_g == pytest.approx(1000.0)
        assert row.total_spools == 1
        assert row.spool_ids == [live.id], "the expanded row lazily fetches LIVE spools"

    @pytest.mark.parametrize(
        ("archived_days_ago", "usage_days_ago", "present"),
        [
            pytest.param(89.0, None, True, id="present_when_archived_89_days_ago"),
            pytest.param(91.0, 91.5, False, id="absent_when_both_marks_are_past_90_days"),
            pytest.param(91.0, 89.0, True, id="a_recent_usage_event_outlives_an_old_archiving"),
        ],
    )
    async def test_archived_only_sku_retention_window(
        self, engine, db_session, archived_days_ago, usage_days_ago, present
    ):
        """An SKU with zero live spools stays for 90 days from
        ``max(last usage event, archived_at)`` — the panel's rule, now the
        engine's (and, via Task 2, finally the alert task's)."""
        spool = await _spool(
            db_session,
            weight_used=500.0,
            created_at=_naive(NOW - timedelta(days=200)),
            archived_at=_naive(NOW - timedelta(days=archived_days_ago)),
        )
        if usage_days_ago is not None:
            await _usage(db_session, spool.id, usage_days_ago, 100.0)

        rows = await _rows_by_key(engine, db_session)
        if present:
            row = rows[_PLA_BLACK]
            assert row.total_spools == 0
            assert row.total_remaining_g == pytest.approx(0.0)
            assert row.spool_ids == []
        else:
            assert _PLA_BLACK not in rows


class TestComputeForecastSettings:
    @pytest.mark.parametrize(
        ("global_days", "sku_days"),
        [pytest.param(10, 3, id="global_wins"), pytest.param(3, 10, id="sku_wins")],
    )
    async def test_the_lead_time_is_the_floor_of_global_and_sku(self, engine, db_session, global_days, sku_days):
        """max(global, sku) — a floor, not a sum, and it works in BOTH directions."""
        db_session.add(Settings(key="forecast_global_lead_time_days", value=str(global_days)))
        db_session.add(
            FilamentSkuSettings(
                material="PLA", subtype=None, brand="Bambu", color_name="Black", lead_time_days=sku_days
            )
        )
        await db_session.commit()
        await _spool(db_session, weight_used=100.0, created_at=_naive(NOW - timedelta(days=10)))

        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.eff_lead_time_days == 10

    async def test_a_colourless_settings_row_still_applies(self, engine, db_session):
        """Pre-colour-grouping rows carry the operator's lead time; the panel
        falls back to them and so must the engine."""
        db_session.add(
            FilamentSkuSettings(material="PLA", subtype=None, brand="Bambu", color_name=None, lead_time_days=10)
        )
        await db_session.commit()
        await _spool(db_session, weight_used=100.0, created_at=_naive(NOW - timedelta(days=10)))

        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.eff_lead_time_days == 10

    async def test_a_snoozed_sku_keeps_its_numbers_and_its_flags(self, engine, db_session):
        db_session.add(Settings(key="forecast_global_lead_time_days", value="7"))
        db_session.add(
            FilamentSkuSettings(material="PLA", subtype=None, brand="Bambu", color_name="Black", alerts_snoozed=True)
        )
        await db_session.commit()
        # 10 g remaining at ~10 g/day (the frozen delta) → deep in stock break.
        await _spool(db_session, label_weight=110, weight_used=100.0, created_at=_naive(NOW - timedelta(days=10)))

        row = (await _rows_by_key(engine, db_session))[_PLA_BLACK]
        assert row.alerts_snoozed is True
        assert row.stock_break_alert is True, "snooze must not blank the computation — consumers filter on it"
        assert row.rate_g_day is not None


# ── The end-to-end parity pin ─────────────────────────────────────────────────


class TestTheParityPin:
    async def test_a_three_sku_farm_end_to_end(self, engine, db_session):
        """The executable spec of record: one history-tier SKU, one delta-tier
        SKU, one archived-only + snoozed SKU — every field asserted, numbers
        composed ONLY from the frozen vectors plus the verified hand arithmetic."""
        db_session.add(Settings(key="forecast_global_lead_time_days", value="4"))
        await db_session.commit()

        # SKU 1 — PLA/Basic/Bambu/Black: two live spools, history tier.
        s1 = await _spool(
            db_session,
            subtype="Basic",
            rgba="000000FF",
            weight_used=65.0,
            created_at=_naive(NOW - timedelta(days=40)),
        )
        s2 = await _spool(
            db_session,
            subtype="Basic",
            rgba="000000FF",
            weight_used=40.0,
            created_at=_naive(NOW - timedelta(days=30)),
        )
        await _seed_vector_usage(db_session, "interleaved_spools_one_sku", {1: s1.id, 2: s2.id})
        db_session.add(
            FilamentSkuSettings(
                material="PLA",
                subtype="Basic",
                brand="Bambu",
                color_name="Black",
                lead_time_days=9,
                safety_margin_value=2,
                safety_margin_unit="days",
            )
        )

        # SKU 2 — PETG/Prusament/Orange: no usage rows, no settings row → delta
        # tier with the client defaults (margin 14 days, global lead time).
        s3 = await _spool(
            db_session,
            material="PETG",
            brand="Prusament",
            color_name="Orange",
            rgba="FF8800FF",
            weight_used=100.0,
            created_at=_naive(NOW - timedelta(days=10)),
        )

        # SKU 3 — ASA/Polymaker/Grey: archived-only (retired yesterday), snoozed.
        s4 = await _spool(
            db_session,
            material="ASA",
            brand="Polymaker",
            color_name="Grey",
            rgba="888888FF",
            weight_used=500.0,
            created_at=_naive(NOW - timedelta(days=40)),
            archived_at=_naive(NOW - timedelta(days=1)),
        )
        await _seed_vector_usage(db_session, "two_days_with_a_five_day_gap", {1: s4.id})
        db_session.add(
            FilamentSkuSettings(
                material="ASA",
                subtype=None,
                brand="Polymaker",
                color_name="Grey",
                lead_time_days=5,
                safety_margin_value=30,
                safety_margin_unit="g",
                alerts_snoozed=True,
            )
        )
        await db_session.commit()

        rows = await _rows_by_key(engine, db_session)
        assert len(rows) == 3

        # ── SKU 1, field by field ────────────────────────────────────────────
        frozen1 = _history_vector("interleaved_spools_one_sku")["expected"]
        r1, sd1 = frozen1["rate"], frozen1["std_dev"]
        safety1 = 1.65 * sd1 * math.sqrt(9) + 2 * r1
        rop1 = r1 * 9 + safety1
        remaining1 = (1000 - 65.0) + (1000 - 40.0)  # 1895
        days1 = math.floor(remaining1 / r1)  # 115
        until1 = math.floor((remaining1 - rop1) / r1)  # 102

        row1 = rows[("PLA", "Basic", "Bambu", "Black")]
        assert row1.rgba == "000000FF"
        assert row1.total_spools == 2
        assert row1.total_remaining_g == pytest.approx(remaining1)
        assert row1.total_label_g == pytest.approx(2000.0)
        assert row1.total_used_g == pytest.approx(105.0)
        assert row1.rate_tier == "history"
        assert row1.rate_g_day == pytest.approx(r1, rel=1e-9)
        assert row1.std_dev == pytest.approx(sd1, rel=1e-9)
        assert row1.eff_lead_time_days == 9  # max(global 4, sku 9)
        assert row1.safety_stock_g == pytest.approx(safety1, rel=1e-6)
        assert row1.reorder_point_g == pytest.approx(rop1, rel=1e-6)
        assert row1.days_remaining == days1
        assert row1.projected_empty_date == TODAY + timedelta(days=days1)
        assert row1.days_until_rop == until1
        assert row1.reorder_trigger_date == TODAY + timedelta(days=until1)
        assert row1.stock_break_alert is False and row1.reorder_alert is False
        assert row1.alerts_snoozed is False
        assert sorted(row1.spool_ids) == sorted([s1.id, s2.id])

        # ── SKU 2 ────────────────────────────────────────────────────────────
        r2 = _delta_vector("one_spool_ten_days")["expected_rate"]  # 10.0
        sigma2 = r2 * 0.2
        safety2 = 1.65 * sigma2 * math.sqrt(4) + 14 * r2  # default margin: 14 days
        rop2 = r2 * 4 + safety2

        row2 = rows[("PETG", None, "Prusament", "Orange")]
        assert row2.rgba == "FF8800FF"
        assert row2.total_spools == 1
        assert row2.total_remaining_g == pytest.approx(900.0)
        assert row2.total_label_g == pytest.approx(1000.0)
        assert row2.total_used_g == pytest.approx(100.0)
        assert row2.rate_tier == "delta"
        assert row2.rate_g_day == pytest.approx(r2, rel=1e-9)
        assert row2.std_dev is None
        assert row2.eff_lead_time_days == 4  # max(global 4, no sku row)
        assert row2.safety_stock_g == pytest.approx(safety2, rel=1e-6)  # 146.6
        assert row2.reorder_point_g == pytest.approx(rop2, rel=1e-6)  # 186.6
        assert row2.days_remaining == 90
        assert row2.projected_empty_date == TODAY + timedelta(days=90)
        assert row2.days_until_rop == 71
        assert row2.reorder_trigger_date == TODAY + timedelta(days=71)
        assert row2.stock_break_alert is False and row2.reorder_alert is False
        assert row2.alerts_snoozed is False
        assert row2.spool_ids == [s3.id]

        # ── SKU 3 ────────────────────────────────────────────────────────────
        frozen3 = _history_vector("two_days_with_a_five_day_gap")["expected"]
        r3, sd3 = frozen3["rate"], frozen3["std_dev"]  # 20.0, 0.0
        safety3 = 1.65 * sd3 * math.sqrt(5) + 30.0  # margin unit g
        rop3 = r3 * 5 + safety3

        row3 = rows[("ASA", None, "Polymaker", "Grey")]
        assert row3.rgba == "888888FF"
        assert row3.total_spools == 0
        assert row3.total_remaining_g == pytest.approx(0.0)
        assert row3.total_label_g == pytest.approx(0.0)
        assert row3.total_used_g == pytest.approx(500.0)
        assert row3.rate_tier == "history", "archived spools ARE the history"
        assert row3.rate_g_day == pytest.approx(r3, rel=1e-9)
        assert row3.std_dev == pytest.approx(sd3, abs=1e-12)
        assert row3.eff_lead_time_days == 5  # max(global 4, sku 5)
        assert row3.safety_stock_g == pytest.approx(safety3, rel=1e-6)  # 30.0
        assert row3.reorder_point_g == pytest.approx(rop3, rel=1e-6)  # 130.0
        assert row3.days_remaining == 0
        assert row3.projected_empty_date == TODAY
        assert row3.days_until_rop == math.floor((0.0 - rop3) / r3)  # -7
        assert row3.reorder_trigger_date == TODAY, "the trigger date clamps at today"
        assert row3.stock_break_alert is True, "0 days left inside a 5-day lead time"
        assert row3.reorder_alert is False, "break wins outright"
        assert row3.alerts_snoozed is True
        assert row3.spool_ids == []
