"""Stock-break alerts — dispatch glue over the forecast engine.

``on_stock_break_alert`` shipped with a provider column, a per-chat Telegram
toggle and en+uk templates, and nothing ever called it: the forecast that
decides the alert ran in ``ForecastPanel.tsx``, in the operator's browser. An
alert that only exists while someone has the Inventory page open is not an
alert — the whole point is to be told *before* the filament runs out, which is
precisely when nobody is looking at the page.

This module used to carry its own line-by-line port of the panel's math. Task 2
of the forecast-server-side plan (2026-08-29) moved that math into
``backend.app.services.forecast_engine`` — the ONE owner, shared with the
forecast endpoints — and left here only what is genuinely about *alerting*:
which SKU rows become messages, when a standing state may repeat itself, and
the per-state notified-at stamps.

Deliberate behavior changes vs the pre-refactor service, each a spec §2.1
ruling (panel parity — where the panel and the old service disagreed, the
panel was right and the service was the bug):

* an SKU whose last spool was archived keeps alerting for 90 days from
  ``max(last usage event, archived_at)`` — the panel's retention rule; the old
  service dropped archived-only SKUs at once, which silenced a colour at the
  exact moment it most needed reordering;
* a ``kg`` safety margin is ``value·1000`` grams — the old else-branch misread
  a stored ``kg`` as *days*;
* the delta-tier rate runs over ALL spools including archived, and the usage
  window is 90 days of time rather than the newest 5000 rows globally;
* under Spoolman the tick exits early — the whole Inventory tab (Forecast
  included) is a Spoolman iframe then, and the local tables this forecast
  reads are not the inventory the operator manages.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import async_session
from backend.app.models.filament_sku_settings import FilamentSkuSettings
from backend.app.models.settings import Settings
from backend.app.services.forecast_engine import (
    DEFAULT_SAFETY_MARGIN_UNIT,
    DEFAULT_SAFETY_MARGIN_VALUE,
    compute_forecast,
    sku_key,
)

logger = logging.getLogger(__name__)

# Which column remembers which announcement.
_STAMP = {"break": "stock_break_notified_at", "reorder": "stock_reorder_notified_at"}


def _as_utc(value: datetime | None) -> datetime | None:
    """Treat a stored naive timestamp as UTC — that is what the API serialises
    and what the browser parses."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


AlertKind = Literal["break", "reorder"]


class SkuAlert(NamedTuple):
    """One SKU that needs the operator's attention, and which kind.

    ``break`` — the filament runs out before a replacement could arrive.
    ``reorder`` — stock has fallen to the reorder point: still enough to cover
    the lead time, but no longer enough to cover it with the safety buffer.

    The two are mutually exclusive, exactly as on the panel (the engine's
    flags already encode "break wins outright"). Once a SKU is genuinely going
    to run out in time, "you should reorder" is no longer the message worth
    sending.
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


async def _spoolman_is_enabled(db: AsyncSession) -> bool:
    raw = (await db.execute(select(Settings.value).where(Settings.key == "spoolman_enabled"))).scalar_one_or_none()
    return (raw or "").lower() == "true"


async def find_stock_alerts(db: AsyncSession, now: datetime | None = None) -> list[SkuAlert]:
    """Every SKU currently in stock break or at its reorder point.

    A thin filter over ``compute_forecast`` — the engine already computed the
    flags; this only translates alerting rows into ``SkuAlert``s. Snoozed SKUs
    are excluded here rather than at the notification step: the panel's snooze
    means "stop telling me about this one", and it would be a poor joke to
    honour that on screen and not in the messages.
    """
    now = now or datetime.now(timezone.utc)

    alerts: list[SkuAlert] = []
    for row in await compute_forecast(db, now=now):
        if row.alerts_snoozed:
            continue
        if row.stock_break_alert:
            kind: AlertKind = "break"
        elif row.reorder_alert:
            kind = "reorder"
        else:
            continue
        # An alerting row always has a positive rate (the engine raises no flag
        # without one), so the int/float fields below are never None.
        alerts.append(
            SkuAlert(
                kind=kind,
                key=sku_key(row.material, row.subtype, row.brand, row.color_name),
                material=row.material,
                subtype=row.subtype,
                brand=row.brand,
                color_name=row.color_name,
                stock_g=row.total_remaining_g,
                rate_g_day=row.rate_g_day,
                days_left=row.days_remaining,
                lead_time_days=row.eff_lead_time_days,
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
            if await _spoolman_is_enabled(db):
                # Under Spoolman the local spool tables are not the inventory —
                # forecasting them would alert on a parallel universe (spec §2.5;
                # the pre-refactor task only survived because the table stayed
                # empty, which is a coincidence, not a guard).
                return

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
                        safety_margin_value=DEFAULT_SAFETY_MARGIN_VALUE,
                        safety_margin_unit=DEFAULT_SAFETY_MARGIN_UNIT,
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
