"""The forecast engine — the ONE owner of the stock-forecast math.

Until Task 2 of the forecast-server-side plan (2026-08-29) this math existed
twice: once in ``frontend/src/components/ForecastPanel.tsx`` (the behavioral
source of truth) and once as a line-by-line port in
``stock_forecast_alerts.py``. Both copies collapse here; the alert task and the
forecast endpoints are consumers of this module, never re-implementations.

Pipeline per call: SQL aggregates (day-bucketed grams per SKU over a 90-day
window + per-SKU stock totals) → small in-memory SKU groups → pure Python
finishing (decay-weighted rate, variance, safety stock, ROP, dates, alert
flags). The post-aggregation set is tens of rows, so "finishing in Python" is
honest, not lazy.

Contract of record: ``backend/tests/test_forecast_engine.py`` (Task 1) — the
frozen ``forecast_vectors/rate_vectors.json`` is the arbiter for the rate math
(measured by executing the pre-refactor implementation); the finishing
constants were verified against ``ForecastPanel.tsx`` before being trusted.

Deliberate deviations from the shipped panel/alert service, each a spec §2.1
ruling:

* the usage window is 90 days of TIME, not the newest 5000 rows globally
  (the global cap silently truncated low-volume SKU history);
* an SKU with zero live spools stays for 90 days from
  ``max(last usage event, archived_at)`` — the panel's retention rule, which
  the alert task now inherits (it used to drop archived-only SKUs at once);
* a ``kg`` safety margin is ``value·1000`` grams (the old alert service
  misread a stored ``kg`` as *days*);
* the delta tier runs over ALL spools including archived (the old alert
  service fed it live spools only);
* dates are UTC calendar days (``now.date()`` in UTC) — the panel anchored
  "today" at local midnight; the server has no viewer timezone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.db_dialect import is_postgres
from backend.app.models.filament_sku_settings import FilamentSkuSettings
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory

# 90 days = three of the rate model's 30-day half-lives; beyond that the decay
# weight is under 12.5%. The same span bounds the archived-only retention, so
# the two windows expire together.
USAGE_WINDOW_DAYS = 90

# 30-day half-life, as in ForecastPanel.
_DECAY_LAMBDA = math.log(2) / 30

# 95% service level, and the spread assumed when the rate came from the delta
# tier and has none of its own. Both are the panel's numbers.
_Z_95 = 1.65
_ASSUMED_SPREAD = 0.2

# The panel keeps the safety stock non-zero for brand-new SKUs: a "days" margin
# with NO rate at all is priced at 5 g/day. A measured rate of exactly 0.0 is
# NOT null — the margin is then honestly 0.
_NO_RATE_PLACEHOLDER_G_DAY = 5.0

# What the UI assumes for an SKU with no settings row.
DEFAULT_SAFETY_MARGIN_VALUE = 14
DEFAULT_SAFETY_MARGIN_UNIT = "days"

GLOBAL_LEAD_TIME_SETTING = "forecast_global_lead_time_days"


def _as_utc(value: datetime | None) -> datetime | None:
    """Treat a stored naive timestamp as UTC — that is what the API serialises
    and what the browser parses."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def sku_key(
    material: str | None, subtype: str | None, brand: str | None, color_name: str | None
) -> tuple[str, str, str, str]:
    """The panel's ``skuKey`` — NULLs collapse to empty strings so a spool with
    no brand groups with the other spools that have no brand. Grouping only:
    the rows this engine returns carry the fields AS STORED (the first spool's,
    by id — ``None`` preserved, never ""-collapsed)."""
    return (material or "", subtype or "", brand or "", color_name or "")


class RateEstimate(NamedTuple):
    """A daily consumption rate and how much it wobbles.

    ``std_dev`` only exists for the history tier — the delta rate is a single
    average with nothing to take a spread of — and it feeds the reorder point's
    statistical safety stock, never the stock-break test.
    """

    rate: float
    std_dev: float


def history_rate(day_buckets: list[tuple[date, float]], now: datetime) -> RateEstimate | None:
    """Time-weighted g/day from UTC-day usage buckets, or None with too little
    to measure.

    The buckets are the SQL layer's output — ``(utc_calendar_day, grams)`` in
    ANY order (GROUP BY guarantees none). Day-bucketing matters: without it,
    two spools of the same SKU printing minutes apart produce a near-zero
    interval and a wildly inflated rate. The arithmetic reproduces the frozen
    golden vectors exactly (inter-day g/day observations, each weighted by
    ``exp(-ln2/30 · age_days)``, weighted mean + weighted std-dev).
    """
    now = _as_utc(now)

    by_day: dict[date, float] = {}
    for day, grams in day_buckets:
        by_day[day] = by_day.get(day, 0.0) + (grams or 0.0)
    if len(by_day) < 2:
        return None

    days = sorted((datetime.combine(day, time(0), tzinfo=timezone.utc), grams) for day, grams in by_day.items())

    observations: list[tuple[float, float]] = []
    for index in range(1, len(days)):
        previous_day, _ = days[index - 1]
        this_day, grams = days[index]
        # Minimum one day, so a same-day reprint is not a zero-length interval.
        elapsed_days = max((this_day - previous_day).total_seconds() / 86400.0, 1.0)
        age_days = (now - this_day).total_seconds() / 86400.0
        observations.append((grams / elapsed_days, math.exp(-_DECAY_LAMBDA * age_days)))

    total_weight = sum(weight for _, weight in observations)
    if total_weight == 0:
        return None

    mean = sum(rate * weight for rate, weight in observations) / total_weight
    variance = sum(weight * (rate - mean) ** 2 for rate, weight in observations) / total_weight
    return RateEstimate(rate=mean, std_dev=math.sqrt(variance))


def delta_rate(total_used_g: float, oldest_created_at: datetime | None, now: datetime) -> float | None:
    """Fallback g/day: consumption over the group's age.

    Takes the aggregates SQL delivers: ``total_used_g`` is
    ``Σ max(0, weight_used − weight_used_baseline)`` clamped PER SPOOL over ALL
    spools of the SKU (archived included — the client passes
    ``group.allSpools``), ``oldest_created_at`` the group's oldest spool (naive
    UTC as the DB stores it). Baseline-aware, so "Reset usage to 0" means the
    rate describes post-reset consumption rather than the spool's whole life.
    """
    if total_used_g <= 0:
        return None
    oldest = _as_utc(oldest_created_at)
    if oldest is None:
        return None
    now = _as_utc(now)
    days_since_oldest = (now - oldest).total_seconds() / 86400.0
    if days_since_oldest < 1:
        return None
    return total_used_g / days_since_oldest


@dataclass
class RowFinish:
    """The client-only arithmetic's output — everything ``finish_row`` decides."""

    safety_stock_g: float
    reorder_point_g: float
    days_remaining: int | None
    projected_empty_date: date | None
    days_until_rop: int | None
    reorder_trigger_date: date | None
    stock_break_alert: bool
    reorder_alert: bool
    alerts_snoozed: bool


def finish_row(
    *,
    rate: float | None,
    std_dev: float | None,
    eff_lead_time_days: int,
    margin_value: float,
    margin_unit: str,
    total_remaining_g: float,
    now: datetime,
    snoozed: bool,
) -> RowFinish:
    """Safety stock, ROP, dates and alert flags — ForecastPanel.tsx verbatim.

    Client truths pinned by the Task 1 suite: Z=1.65; σ = std_dev if present
    else rate·0.2 (else 0); margin units are THREE (g / kg·1000 / days·rate,
    with the 5 g/day placeholder only at rate None); ROP forced to 0 (not None)
    at rate None while the safety stock stays real; both day counts floor();
    both alert boundaries inclusive; the stock break needs a lead time > 0;
    reorder is mutually exclusive with break (break wins); the trigger date
    clamps at today while ``days_until_rop`` keeps its raw negative. Dates are
    UTC calendar days.
    """
    today = _as_utc(now).date()

    sigma = std_dev if std_dev is not None else (rate * _ASSUMED_SPREAD if rate is not None else 0.0)
    if margin_unit == "g":
        margin_g = float(margin_value)
    elif margin_unit == "kg":
        margin_g = float(margin_value) * 1000.0
    else:  # "days"
        margin_g = (rate if rate is not None else _NO_RATE_PLACEHOLDER_G_DAY) * margin_value

    safety_stock_g = _Z_95 * sigma * math.sqrt(eff_lead_time_days) + margin_g
    reorder_point_g = 0.0 if rate is None else rate * eff_lead_time_days + safety_stock_g

    if rate is not None and rate > 0:
        days_remaining = math.floor(total_remaining_g / rate)
        projected_empty_date = today + timedelta(days=days_remaining)
        days_until_rop = math.floor((total_remaining_g - reorder_point_g) / rate)
        reorder_trigger_date = today + timedelta(days=max(0, days_until_rop))
        stock_break_alert = eff_lead_time_days > 0 and days_remaining <= eff_lead_time_days
        reorder_alert = (not stock_break_alert) and days_until_rop <= 0
    else:
        days_remaining = None
        projected_empty_date = None
        days_until_rop = None
        reorder_trigger_date = None
        stock_break_alert = False
        reorder_alert = False

    return RowFinish(
        safety_stock_g=safety_stock_g,
        reorder_point_g=reorder_point_g,
        days_remaining=days_remaining,
        projected_empty_date=projected_empty_date,
        days_until_rop=days_until_rop,
        reorder_trigger_date=reorder_trigger_date,
        stock_break_alert=stock_break_alert,
        reorder_alert=reorder_alert,
        alerts_snoozed=snoozed,
    )


@dataclass
class SkuForecastRow:
    """One fully finished SKU row — what every consumer renders or alerts on."""

    material: str | None
    subtype: str | None
    brand: str | None
    color_name: str | None
    rgba: str | None
    total_spools: int
    total_remaining_g: float
    total_label_g: float
    # Mean ``label_weight`` over EVERY spool of the SKU, archived included —
    # "how big is a spool of this SKU", which is a property of the SKU and not
    # of what is on the shelf right now. The one archived-inclusive weight on
    # this row: every other total is live-gated because it describes STOCK, and
    # an archived-only SKU (kept alerting by the 90-day retention window) would
    # otherwise have no recoverable spool size at all. ``None``, never 0, when
    # no spool of the SKU carries a label weight — the consumer's own fallback
    # is a better guess than a fabricated size.
    avg_spool_label_g: float | None
    total_used_g: float
    rate_g_day: float | None
    rate_tier: str  # "history" | "delta" | "none"
    std_dev: float | None
    eff_lead_time_days: int
    safety_stock_g: float | None
    reorder_point_g: float | None
    days_remaining: int | None
    projected_empty_date: date | None
    days_until_rop: int | None
    reorder_trigger_date: date | None
    stock_break_alert: bool
    reorder_alert: bool
    alerts_snoozed: bool
    spool_ids: list[int]


def _day_bucket_expr():
    """A cross-dialect 'YYYY-MM-DD' of the naive-UTC ``created_at``.

    The column stores naive UTC, so the string prefix IS the UTC calendar day
    on both engines. ``func.date()`` is not portable (PostgreSQL has no such
    function) and ``CAST(... AS DATE)`` is garbage on SQLite (no DATE type),
    hence the dialect branch — the same split ``migrations/helpers.py`` makes.
    """
    if is_postgres():
        return func.to_char(SpoolUsageHistory.created_at, "YYYY-MM-DD")
    return func.strftime("%Y-%m-%d", SpoolUsageHistory.created_at)


def _day_bucket_query(window_start: datetime, *, exclude_reset_spools: bool):
    """Day-bucketed grams per raw SKU tuple since ``window_start``.

    The GROUP BY runs over the RAW columns — the NULL→"" collapse happens in
    Python when the aggregate rows merge into engine groups, so the DB never
    has to answer the identity question.
    """
    day = _day_bucket_expr().label("day")
    query = (
        select(
            Spool.material,
            Spool.subtype,
            Spool.brand,
            Spool.color_name,
            day,
            func.sum(func.coalesce(SpoolUsageHistory.weight_used, 0.0)).label("grams"),
        )
        .select_from(SpoolUsageHistory)
        .join(Spool, Spool.id == SpoolUsageHistory.spool_id)
        .where(SpoolUsageHistory.created_at >= window_start)
        .group_by(Spool.material, Spool.subtype, Spool.brand, Spool.color_name, day)
    )
    if exclude_reset_spools:
        # A reset spool's pre-reset events have no anchor and would inflate the
        # rate — the WHOLE spool leaves the history tier (client parity).
        query = query.where(func.coalesce(Spool.weight_used_baseline, 0.0) == 0.0)
    return query


async def _global_lead_time_days(db: AsyncSession) -> int:
    raw = (
        await db.execute(select(Settings.value).where(Settings.key == GLOBAL_LEAD_TIME_SETTING))
    ).scalar_one_or_none()
    if raw is None:
        return 0
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


class _Group:
    """Accumulator for one collapsed SKU key while the query results merge."""

    __slots__ = (
        "fields",
        "live_spools",
        "all_spools",
        "remaining_g",
        "label_g",
        "all_label_g",
        "used_g",
        "oldest_created_at",
        "newest_archived_at",
        "spool_ids",
        "rgba_live",
        "rgba_archived",
        "buckets",
        "last_usage_at",
    )

    def __init__(self) -> None:
        self.fields: tuple[str | None, str | None, str | None, str | None] | None = None
        self.live_spools = 0
        self.all_spools = 0
        self.remaining_g = 0.0
        self.label_g = 0.0
        self.all_label_g = 0.0
        self.used_g = 0.0
        self.oldest_created_at: datetime | None = None
        self.newest_archived_at: datetime | None = None
        self.spool_ids: list[int] = []
        self.rgba_live: str | None = None
        self.rgba_archived: str | None = None
        self.buckets: dict[date, float] = {}
        self.last_usage_at: datetime | None = None


async def compute_forecast(db: AsyncSession, *, now: datetime | None = None) -> list[SkuForecastRow]:
    """Every SKU's forecast, fully finished — the module's one entry point.

    Consumers: the forecast endpoints (Task 3) and the 6-hour alert task.
    """
    now = _as_utc(now) or datetime.now(timezone.utc)
    now_naive = now.astimezone(timezone.utc).replace(tzinfo=None)
    window_start = now_naive - timedelta(days=USAGE_WINDOW_DAYS)

    # ── SQL layer ────────────────────────────────────────────────────────────
    # Per-spool clamps stay INSIDE the SUM: clamping after summing would let an
    # over-reset spool's negative eat a healthy spool's grams (the frozen
    # ``over_reset_spool_does_not_poison_the_group`` vector = 10.0 discriminates
    # against exactly that).
    used_expr = func.coalesce(Spool.weight_used, 0.0) - func.coalesce(Spool.weight_used_baseline, 0.0)
    remaining_expr = func.coalesce(Spool.label_weight, 0) - func.coalesce(Spool.weight_used, 0.0)
    is_live = Spool.archived_at.is_(None)

    totals_rows = (
        await db.execute(
            select(
                Spool.material,
                Spool.subtype,
                Spool.brand,
                Spool.color_name,
                func.sum(case((is_live, 1), else_=0)).label("live_spools"),
                func.count(Spool.id).label("all_spools"),
                func.sum(case((is_live & (remaining_expr > 0), remaining_expr), else_=0.0)).label("remaining_g"),
                func.sum(case((is_live, func.coalesce(Spool.label_weight, 0)), else_=0)).label("label_g"),
                # UNGATED, unlike ``label_g`` — the mean spool size below is the
                # SKU's property, not the shelf's (see ``avg_spool_label_g``).
                func.sum(func.coalesce(Spool.label_weight, 0)).label("all_label_g"),
                func.sum(case((used_expr > 0, used_expr), else_=0.0)).label("used_g"),
                func.min(Spool.created_at).label("oldest_created_at"),
                func.max(Spool.archived_at).label("newest_archived_at"),
            ).group_by(Spool.material, Spool.subtype, Spool.brand, Spool.color_name)
        )
    ).all()

    # Slim per-spool rows, id-ordered: the live ids for the expanded row, the
    # deterministic swatch pick, and the as-stored field reporting (the shipped
    # service reported ``group[0]``'s fields — first spool wins; ORDER BY id
    # replaces its accidental feed order with a stable one).
    spool_rows = (
        await db.execute(
            select(
                Spool.id,
                Spool.material,
                Spool.subtype,
                Spool.brand,
                Spool.color_name,
                Spool.rgba,
                Spool.archived_at,
            ).order_by(Spool.id)
        )
    ).all()

    # Retention needs an UN-windowed newest-usage mark: an archived-only SKU
    # whose newest event sits at day 91 must be dropped because the mark is OLD
    # — a windowed MAX would return NULL and misdecide (Task 1 report).
    last_usage_rows = (
        await db.execute(
            select(
                Spool.material,
                Spool.subtype,
                Spool.brand,
                Spool.color_name,
                func.max(SpoolUsageHistory.created_at).label("last_usage_at"),
            )
            .select_from(SpoolUsageHistory)
            .join(Spool, Spool.id == SpoolUsageHistory.spool_id)
            .group_by(Spool.material, Spool.subtype, Spool.brand, Spool.color_name)
        )
    ).all()

    history_rows = (await db.execute(_day_bucket_query(window_start, exclude_reset_spools=True))).all()

    global_lead_time = await _global_lead_time_days(db)
    settings_rows = list((await db.execute(select(FilamentSkuSettings))).scalars().all())
    settings_by_key = {sku_key(r.material, r.subtype, r.brand, r.color_name): r for r in settings_rows}

    # ── Merge the raw-tuple aggregates into collapsed groups ─────────────────
    groups: dict[tuple[str, str, str, str], _Group] = {}

    def _group(material, subtype, brand, color_name) -> _Group:
        return groups.setdefault(sku_key(material, subtype, brand, color_name), _Group())

    for row in totals_rows:
        g = _group(row.material, row.subtype, row.brand, row.color_name)
        g.live_spools += int(row.live_spools or 0)
        g.all_spools += int(row.all_spools or 0)
        g.remaining_g += float(row.remaining_g or 0.0)
        g.label_g += float(row.label_g or 0.0)
        g.all_label_g += float(row.all_label_g or 0.0)
        g.used_g += float(row.used_g or 0.0)
        if row.oldest_created_at is not None:
            g.oldest_created_at = (
                row.oldest_created_at
                if g.oldest_created_at is None
                else min(g.oldest_created_at, row.oldest_created_at)
            )
        if row.newest_archived_at is not None:
            g.newest_archived_at = (
                row.newest_archived_at
                if g.newest_archived_at is None
                else max(g.newest_archived_at, row.newest_archived_at)
            )

    for row in spool_rows:
        g = _group(row.material, row.subtype, row.brand, row.color_name)
        if g.fields is None:
            g.fields = (row.material, row.subtype, row.brand, row.color_name)
        if row.archived_at is None:
            g.spool_ids.append(row.id)
            if g.rgba_live is None and row.rgba:
                g.rgba_live = row.rgba
        elif g.rgba_archived is None and row.rgba:
            g.rgba_archived = row.rgba

    for row in last_usage_rows:
        g = _group(row.material, row.subtype, row.brand, row.color_name)
        if row.last_usage_at is not None:
            g.last_usage_at = row.last_usage_at if g.last_usage_at is None else max(g.last_usage_at, row.last_usage_at)

    for row in history_rows:
        g = _group(row.material, row.subtype, row.brand, row.color_name)
        day = date.fromisoformat(str(row.day))
        g.buckets[day] = g.buckets.get(day, 0.0) + float(row.grams or 0.0)

    # ── Finish every retained group ──────────────────────────────────────────
    rows: list[SkuForecastRow] = []
    for key in sorted(groups):
        g = groups[key]
        if g.fields is None:  # pragma: no cover — every group came from spool rows
            continue

        if g.live_spools == 0:
            # Archived-only retention: 90 days from max(last usage, archived_at),
            # inclusive at the boundary like the panel (``lastTouchedMs >= cutoff``).
            marks = [m for m in (g.last_usage_at, g.newest_archived_at) if m is not None]
            if not marks or max(marks) < window_start:
                continue

        estimate = history_rate(sorted(g.buckets.items()), now) if g.buckets else None
        if estimate is not None:
            rate: float | None = estimate.rate
            std_dev: float | None = estimate.std_dev
            tier = "history"
        else:
            fallback = delta_rate(g.used_g, g.oldest_created_at, now)
            if fallback is not None:
                rate, std_dev, tier = fallback, None, "delta"
            else:
                rate, std_dev, tier = None, None, "none"

        settings_row = settings_by_key.get(key)
        if settings_row is None and key[3]:
            # Pre-colour-grouping rows carry the operator's lead time; the panel
            # falls back to them and so must the engine (snooze rides along).
            settings_row = settings_by_key.get((key[0], key[1], key[2], ""))

        eff_lead_time_days = max(global_lead_time, settings_row.lead_time_days if settings_row else 0)
        margin_value = settings_row.safety_margin_value if settings_row else DEFAULT_SAFETY_MARGIN_VALUE
        margin_unit = settings_row.safety_margin_unit if settings_row else DEFAULT_SAFETY_MARGIN_UNIT
        snoozed = bool(settings_row.alerts_snoozed) if settings_row else False

        finish = finish_row(
            rate=rate,
            std_dev=std_dev,
            eff_lead_time_days=eff_lead_time_days,
            margin_value=margin_value,
            margin_unit=margin_unit,
            total_remaining_g=g.remaining_g,
            now=now,
            snoozed=snoozed,
        )

        # The client's ``allSpools`` mean, with the zero refused: a SKU whose
        # every spool carries label_weight 0 has no knowable spool size, and 0
        # would divide-by-zero (or suggest an infinity of spools) downstream.
        avg_label = (g.all_label_g / g.all_spools) if g.all_spools else 0.0
        avg_spool_label_g = avg_label if avg_label > 0 else None

        material, subtype, brand, color_name = g.fields
        rows.append(
            SkuForecastRow(
                material=material,
                subtype=subtype,
                brand=brand,
                color_name=color_name,
                rgba=g.rgba_live or g.rgba_archived,
                total_spools=g.live_spools,
                total_remaining_g=g.remaining_g,
                total_label_g=g.label_g,
                avg_spool_label_g=avg_spool_label_g,
                total_used_g=g.used_g,
                rate_g_day=rate,
                rate_tier=tier,
                std_dev=std_dev,
                eff_lead_time_days=eff_lead_time_days,
                safety_stock_g=finish.safety_stock_g,
                reorder_point_g=finish.reorder_point_g,
                days_remaining=finish.days_remaining,
                projected_empty_date=finish.projected_empty_date,
                days_until_rop=finish.days_until_rop,
                reorder_trigger_date=finish.reorder_trigger_date,
                stock_break_alert=finish.stock_break_alert,
                reorder_alert=finish.reorder_alert,
                alerts_snoozed=finish.alerts_snoozed,
                spool_ids=g.spool_ids,
            )
        )

    return rows


async def usage_day_series(
    db: AsyncSession,
    *,
    sku_keys: list[tuple[str | None, str | None, str | None, str | None]],
    days: int,
    now: datetime | None = None,
) -> dict[tuple, list[tuple[date, float]]]:
    """Day-bucketed usage grams for the requested SKUs over the last ``days``
    days — the chart's raw series, straight off the same SQL layer.

    Keys are matched NULL→""-collapsed (``sku_key``); the result is keyed by
    the exact tuples the caller passed, each value date-sorted. Reset spools
    are deliberately NOT excluded here: this is the record of what was burned,
    not the rate model's input (the exclusion exists only because pre-reset
    events have no anchor for a RATE).
    """
    now = _as_utc(now) or datetime.now(timezone.utc)
    window_start = now.astimezone(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    wanted = {sku_key(*key): key for key in sku_keys}
    series: dict[tuple, dict[date, float]] = {key: {} for key in sku_keys}

    rows = (await db.execute(_day_bucket_query(window_start, exclude_reset_spools=False))).all()
    for row in rows:
        target = wanted.get(sku_key(row.material, row.subtype, row.brand, row.color_name))
        if target is None:
            continue
        day = date.fromisoformat(str(row.day))
        bucket = series[target]
        bucket[day] = bucket.get(day, 0.0) + float(row.grams or 0.0)

    return {key: sorted(buckets.items()) for key, buckets in series.items()}
