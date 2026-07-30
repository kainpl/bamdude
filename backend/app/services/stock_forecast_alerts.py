"""Stock-break alerts — the scheduled aggregator m059 left a note about.

``on_stock_break_alert`` shipped with a provider column, a per-chat Telegram
toggle and en+uk templates, and nothing ever called it: the forecast that decides
the alert runs in ``ForecastPanel.tsx``, in the operator's browser. An alert that
only exists while someone has the Inventory page open is not an alert — the whole
point is to be told *before* the filament runs out, which is precisely when
nobody is looking at the page.

So the forecast moves here, and the arithmetic is a **deliberate line-by-line
port** of the panel's, not an improvement on it:

* SKUs are the panel's tuple — material, subtype, brand, colour — over
  non-archived spools.
* The daily rate is the panel's two-tier estimate. First choice is the
  history rate: usage records bucketed by UTC calendar day, inter-day g/day
  observations, each weighted by ``exp(-ln2/30 * age_days)`` so a print from a
  month ago counts half. Fallback is the delta rate: consumption since the
  ``weight_used_baseline`` reset, over the age of the oldest spool in the group.
* Only spools with a zero baseline contribute history, because a reset leaves
  its earlier records with no anchor and they would inflate the rate.
* Lead time is ``max(global, per-SKU)``, and a SKU with no settings row falls
  back to the colour-less row so pre-colour-grouping overrides still count.
* A **stock break** is ``days_remaining <= lead_time`` with a lead time set at
  all: the filament runs out before a replacement could arrive.

Where the panel and this file disagree, the panel is right and this file is the
bug — an alert that contradicts the screen it came from is worse than no alert.
The one thing deliberately not ported is the statistical safety stock (Z·σ·√L):
it feeds the *reorder* alert, which is a separate toggle with no service behind
it, and the stock-break test never reads it.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Literal, NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import async_session
from backend.app.models.filament_sku_settings import FilamentSkuSettings
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory

logger = logging.getLogger(__name__)

# 30-day half-life, as in ForecastPanel.
_DECAY_LAMBDA = math.log(2) / 30

# The panel asks for the 5000 most recent usage rows and forecasts from those.
# Matching the cap keeps the two answers identical on any real farm rather than
# "more correct here, different there".
_USAGE_HISTORY_LIMIT = 5000

_DEFAULT_SAFETY_MARGIN_VALUE = 14

# 95% service level, and the spread the panel assumes when the rate came from the
# delta tier and has none of its own. Both are the panel's numbers.
_Z_95 = 1.65
_ASSUMED_SPREAD = 0.2

# Which column remembers which announcement.
_STAMP = {"break": "stock_break_notified_at", "reorder": "stock_reorder_notified_at"}


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
    no brand groups with the other spools that have no brand."""
    return (material or "", subtype or "", brand or "", color_name or "")


class RateEstimate(NamedTuple):
    """A daily consumption rate and how much it wobbles.

    ``std_dev`` only exists for the history tier — the delta rate is a single
    average with nothing to take a spread of — and it feeds the reorder point's
    statistical safety stock, never the stock-break test.
    """

    rate: float
    std_dev: float


def history_rate(records: list[SpoolUsageHistory], now: datetime) -> RateEstimate | None:
    """Time-weighted g/day from usage history, or None with too little to measure.

    Port of ``computeHistoryRate``. Day-bucketing matters: without it, two spools
    of the same SKU printing minutes apart produce a near-zero interval and a
    wildly inflated rate.
    """
    if len(records) < 2:
        return None

    by_day: dict[str, float] = {}
    for record in records:
        created = _as_utc(record.created_at)
        if created is None:
            continue
        by_day[created.strftime("%Y-%m-%d")] = by_day.get(created.strftime("%Y-%m-%d"), 0.0) + (
            record.weight_used or 0.0
        )

    if len(by_day) < 2:
        return None

    days = sorted(
        (datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc), grams) for day, grams in by_day.items()
    )

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


def delta_rate(spools: list[Spool], now: datetime) -> float | None:
    """Fallback g/day: consumption since the usage reset over the group's age.

    Port of ``computeDeltaRate``. Baseline-aware, so "Reset usage to 0" means the
    rate describes post-reset consumption rather than the spool's whole life.
    """
    total_used = sum(max(0.0, (s.weight_used or 0.0) - (s.weight_used_baseline or 0.0)) for s in spools)
    if total_used == 0:
        return None

    created = [c for c in (_as_utc(s.created_at) for s in spools) if c is not None]
    if not created:
        return None
    days_since_oldest = (now - min(created)).total_seconds() / 86400.0
    if days_since_oldest < 1:
        return None
    return total_used / days_since_oldest


AlertKind = Literal["break", "reorder"]


class SkuAlert(NamedTuple):
    """One SKU that needs the operator's attention, and which kind.

    ``break`` — the filament runs out before a replacement could arrive.
    ``reorder`` — stock has fallen to the reorder point: still enough to cover
    the lead time, but no longer enough to cover it with the safety buffer.

    The two are mutually exclusive, exactly as on the panel. Once a SKU is
    genuinely going to run out in time, "you should reorder" is no longer the
    message worth sending.
    """

    kind: AlertKind
    key: tuple[str, str, str, str]
    material: str
    subtype: str | None
    brand: str | None
    color_name: str | None
    stock_g: float
    rate_g_day: float
    days_left: int
    lead_time_days: int


async def _global_lead_time_days(db: AsyncSession) -> int:
    raw = (
        await db.execute(select(Settings.value).where(Settings.key == "forecast_global_lead_time_days"))
    ).scalar_one_or_none()
    if raw is None:
        return 0
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


async def find_stock_alerts(db: AsyncSession, now: datetime | None = None) -> list[SkuAlert]:
    """Every SKU currently in stock break or at its reorder point.

    Snoozed SKUs are excluded here rather than at the notification step: the
    panel's snooze means "stop telling me about this one", and it would be a poor
    joke to honour that on screen and not in the messages.
    """
    now = now or datetime.now(timezone.utc)

    global_lead_time = await _global_lead_time_days(db)

    spools = list((await db.execute(select(Spool).where(Spool.archived_at.is_(None)))).scalars().all())
    if not spools:
        return []

    settings_rows = list((await db.execute(select(FilamentSkuSettings))).scalars().all())
    settings_by_key = {sku_key(row.material, row.subtype, row.brand, row.color_name): row for row in settings_rows}

    history = list(
        (
            await db.execute(
                select(SpoolUsageHistory).order_by(SpoolUsageHistory.created_at.desc()).limit(_USAGE_HISTORY_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    history_by_spool: dict[int, list[SpoolUsageHistory]] = {}
    for record in history:
        history_by_spool.setdefault(record.spool_id, []).append(record)

    groups: dict[tuple[str, str, str, str], list[Spool]] = {}
    for spool in spools:
        groups.setdefault(sku_key(spool.material, spool.subtype, spool.brand, spool.color_name), []).append(spool)

    alerts: list[SkuAlert] = []
    for key, group in groups.items():
        material, subtype, brand, color_name = (
            group[0].material,
            group[0].subtype,
            group[0].brand,
            group[0].color_name,
        )

        row = settings_by_key.get(key)
        if row is None and color_name:
            # Pre-colour-grouping rows carry the operator's lead time; the panel
            # falls back to them and so must this.
            row = settings_by_key.get(sku_key(material, subtype, brand, None))
        if row is not None and row.alerts_snoozed:
            continue

        lead_time_days = max(global_lead_time, row.lead_time_days if row else 0)
        remaining_g = sum(max(0.0, (s.label_weight or 0) - (s.weight_used or 0.0)) for s in group)

        group_history: list[SpoolUsageHistory] = []
        for spool in group:
            if (spool.weight_used_baseline or 0) == 0:
                group_history.extend(history_by_spool.get(spool.id, []))

        estimate = history_rate(group_history, now)
        if estimate is not None:
            rate, std_dev = estimate.rate, estimate.std_dev
        else:
            fallback = delta_rate(group, now)
            if fallback is None:
                continue
            # The delta tier is a single average with no spread to measure, so
            # the panel assumes 20% — carried over rather than invented here.
            rate, std_dev = fallback, fallback * _ASSUMED_SPREAD
        if rate <= 0:
            continue

        days_left = math.floor(remaining_g / rate)

        # Stock break wins outright: a lead time must be configured for the
        # phrase "before replenishment arrives" to mean anything at all.
        if lead_time_days > 0 and days_left <= lead_time_days:
            kind: AlertKind = "break"
        else:
            # Reorder point = what the lead time will eat, plus a buffer for the
            # rate being an estimate (Z·σ·√L) and the operator's own margin.
            margin_value = row.safety_margin_value if row else _DEFAULT_SAFETY_MARGIN_VALUE
            margin_unit = row.safety_margin_unit if row else "days"
            safety_margin_g = margin_value if margin_unit == "g" else rate * margin_value
            safety_stock_g = _Z_95 * std_dev * math.sqrt(lead_time_days) + safety_margin_g
            reorder_point_g = rate * lead_time_days + safety_stock_g
            if math.floor((remaining_g - reorder_point_g) / rate) > 0:
                continue
            kind = "reorder"

        alerts.append(
            SkuAlert(
                kind=kind,
                key=key,
                material=material,
                subtype=subtype,
                brand=brand,
                color_name=color_name,
                stock_g=remaining_g,
                rate_g_day=rate,
                days_left=days_left,
                lead_time_days=lead_time_days,
            )
        )

    return alerts


class StockForecastAlerts:
    """Background loop that announces stock breaks."""

    # Stock runs out on a scale of days; six hours is often enough to catch a
    # break the morning it appears without waking anyone up over it.
    _check_interval = 6 * 60 * 60
    # A standing break re-states itself at most this often. The message says
    # "order immediately", so a daily nudge is the point — but only daily.
    _repeat_after = timedelta(days=1)

    def __init__(self) -> None:
        self._running = False

    async def run(self) -> None:
        self._running = True
        logger.info("Stock-forecast alerts started (interval=%ds)", self._check_interval)
        while self._running:
            try:
                await self.tick()
            except Exception:
                # One bad pass must not take the loop down with it.
                logger.exception("StockForecastAlerts tick failed")
            await asyncio.sleep(self._check_interval)

    def stop(self) -> None:
        self._running = False
        logger.info("Stock-forecast alerts stopped")

    async def tick(self) -> None:
        async with async_session() as db:
            now = datetime.now(timezone.utc)
            alerts = await find_stock_alerts(db, now)
            active: dict[AlertKind, set[tuple[str, str, str, str]]] = {
                "break": {a.key for a in alerts if a.kind == "break"},
                "reorder": {a.key for a in alerts if a.kind == "reorder"},
            }

            # Clear the stamp of every state a SKU is no longer in, so both a
            # recovery and a slide from reorder into break are announced at once
            # instead of waiting out a window that belongs to the other state.
            rows = list((await db.execute(select(FilamentSkuSettings))).scalars().all())
            by_key = {sku_key(r.material, r.subtype, r.brand, r.color_name): r for r in rows}
            for key, row in by_key.items():
                for kind in ("break", "reorder"):
                    if getattr(row, _STAMP[kind]) is not None and key not in active[kind]:
                        setattr(row, _STAMP[kind], None)

            announced = 0
            for entry in alerts:
                row = by_key.get(entry.key)
                last = _as_utc(getattr(row, _STAMP[entry.kind])) if row else None
                if last is not None and now - last < self._repeat_after:
                    continue

                if row is None:
                    # No settings row yet: create one carrying exactly the
                    # defaults the UI already assumes for a missing row, so the
                    # operator sees no change beyond the alert itself.
                    row = FilamentSkuSettings(
                        material=entry.material,
                        subtype=entry.subtype,
                        brand=entry.brand,
                        color_name=entry.color_name,
                        lead_time_days=0,
                        safety_margin_value=_DEFAULT_SAFETY_MARGIN_VALUE,
                        safety_margin_unit="days",
                        alerts_snoozed=False,
                    )
                    db.add(row)
                    by_key[entry.key] = row
                setattr(row, _STAMP[entry.kind], now)

                await self._announce(db, entry)
                announced += 1

            await db.commit()
            if announced:
                logger.info(
                    "Stock-forecast alerts: announced %d SKU(s) (%d break, %d reorder)",
                    announced,
                    len(active["break"]),
                    len(active["reorder"]),
                )

    async def _announce(self, db: AsyncSession, entry: SkuAlert) -> None:
        from backend.app.services.notification_service import notification_service

        common = {
            "material": entry.material,
            "brand": entry.brand,
            "stock_g": entry.stock_g,
            "rate_g_day": entry.rate_g_day,
            "days_left": entry.days_left,
            "db": db,
        }
        try:
            if entry.kind == "break":
                # Only the break template renders the lead time — it is the whole
                # point of that message ("runs out before replenishment arrives");
                # the reorder one talks about the reorder point instead.
                await notification_service.on_stock_break_alert(lead_time_days=entry.lead_time_days, **common)
            else:
                await notification_service.on_stock_reorder_alert(**common)
        except Exception:
            # The stamp is still written: a provider that is down must not turn
            # into a message every six hours once it comes back.
            logger.exception("Stock %s notification failed for %s", entry.kind, entry.material)


stock_forecast_alerts = StockForecastAlerts()
