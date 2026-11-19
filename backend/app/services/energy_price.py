"""The electricity price over time, and the cost of energy at the price of its own time (m245).

``energy_cost_per_kwh`` in the settings is the price NOW -- the fixed one, or the last one read from
a Home Assistant sensor. ``energy_price_history`` keeps every change of it with the moment it took
effect; this module is the table's ONE writer (``tests/unit/test_energy_price_history_has_one_writer.py``)
and the one place that answers "what did a kWh cost then".

Three rules everything here follows:

* the price of a moment is the newest history row at or before it; the ``baseline`` row lies far in
  the past, so everything before the upgrade costs what the price was on the upgrade day;
* energy is costed step by step along one meter source's counter (``cost_of_steps``): a positive
  step at the price in effect when the step STARTED, a backward step (the counter was reset) as
  nothing -- neither energy nor cost;
* a change is recorded in the transaction that changes the setting, and only when the price really
  differs from the standing one. Changing the price never re-costs the past.

The Home Assistant reader follows the integration-credential rule: the connection and the entity are
read and frozen in a short transaction, that transaction ends, THEN the network is asked, and the
price is written in a new short transaction. It never runs on the print path: a print is costed from
the database alone.
"""

from __future__ import annotations

import logging
import math
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select

from backend.app.models.energy_price_history import EnergyPriceHistory

logger = logging.getLogger(__name__)

SETTING_KEY = "energy_cost_per_kwh"
SOURCE_SETTING_KEY = "energy_price_source"
ENTITY_SETTING_KEY = "energy_price_ha_entity"
#: ``AppSettings.energy_cost_per_kwh``'s default: the price of an installation that never set one.
DEFAULT_PRICE = 0.15
#: Where the baseline row lies: before any reading this installation can hold (as m245 seeds it).
BASELINE_AT = datetime(1970, 1, 1)

PRICE_SOURCE_FIXED = "fixed"
PRICE_SOURCE_HOMEASSISTANT = "homeassistant"
PRICE_SOURCES = (PRICE_SOURCE_FIXED, PRICE_SOURCE_HOMEASSISTANT)

#: Who changed the price (the table's CHECK).
CHANGE_SOURCES = ("baseline", "manual", "api", "homeassistant", "restore")

#: How a print's cost was reached (``energy_provenance["tariff_method"]``).
METHOD_STEPS = "steps"  # along the counter, each step at the price of its start
METHOD_WINDOW_AVERAGE = "window_average"  # an estimate: the time-weighted price of the measured window
METHOD_SINGLE = "single"  # no point between start and end: one step at the price of its start

HA_TIMEOUT_SECONDS = 5.0
#: A sensor's unit after the currency, scaled to per kWh. Day-ahead sensors are often per MWh.
_UNIT_SCALE = {"kwh": 1.0, "mwh": 0.001, "wh": 1000.0}


@dataclass(frozen=True)
class PricePoint:
    """The price from ``effective_at`` (naive UTC) until the next point."""

    effective_at: datetime
    price: float


@dataclass(frozen=True)
class StepCost:
    """The positive energy of a run of counter points, and what it cost."""

    kwh: float
    cost: float

    def __add__(self, other: StepCost) -> StepCost:
        return StepCost(self.kwh + other.kwh, self.cost + other.cost)


NO_STEPS = StepCost(0.0, 0.0)


def naive_utc(moment: datetime | None = None) -> datetime:
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def clean_price(raw) -> float:
    """A stored or sent price as the number energy is costed at.

    An unreadable or non-finite value reads as the default -- exactly what the settings reader makes of
    such a row -- and a negative one as free: a cost below zero is not something any reader expects.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_PRICE
    if not math.isfinite(value):
        return DEFAULT_PRICE
    return max(0.0, value)


# ── Reading ──────────────────────────────────────────────────────────────────


def price_at(history: Sequence[PricePoint], moment: datetime) -> float:
    """The price in effect at ``moment``: the newest point at or before it.

    ``history`` is in time order (``load_history``). A moment before the first point takes the first
    point's price -- there is nothing earlier to know.
    """
    if not history:
        return DEFAULT_PRICE
    moment = naive_utc(moment)
    index = bisect_right(history, moment, key=lambda point: point.effective_at)
    return history[max(0, index - 1)].price


def average_price(history: Sequence[PricePoint], start: datetime, end: datetime) -> float:
    """The time-weighted price over ``[start, end]``; the price at ``start`` for an empty window."""
    start, end = naive_utc(start), naive_utc(end)
    if end <= start:
        return price_at(history, start)
    total = 0.0
    cursor, price = start, price_at(history, start)
    for point in history:
        if point.effective_at <= start:
            continue
        if point.effective_at >= end:
            break
        total += price * (point.effective_at - cursor).total_seconds()
        cursor, price = point.effective_at, point.price
    total += price * (end - cursor).total_seconds()
    return total / (end - start).total_seconds()


def cost_of_steps(points: Iterable[tuple[datetime, float]], history: Sequence[PricePoint]) -> StepCost:
    """Energy and cost along ``points`` -- (moment, lifetime kWh) of ONE meter source, in time order.

    Each positive step is costed at the price in effect at the step's START. A backward step is a
    counter reset: no energy and no cost, and the next step starts from the lower value.
    """
    kwh = cost = 0.0
    previous: tuple[datetime, float] | None = None
    for moment, reading in points:
        if previous is not None:
            step = reading - previous[1]
            if step > 0:
                kwh += step
                cost += step * price_at(history, previous[0])
        previous = (moment, reading)
    return StepCost(kwh, cost)


def price_of(steps: StepCost, fallback: float) -> float:
    """The average price of the energy ``steps`` could cost; ``fallback`` when there was none.

    A figure reached another way (a counter difference clamped over a reset, a live total) is costed
    at this price, so the figure stays what it was and only its money follows the steps.
    """
    return steps.cost / steps.kwh if steps.kwh > 0 else fallback


async def print_price(
    db,
    plug_id: int,
    history: Sequence[PricePoint],
    *,
    start_at: datetime | None,
    start_kwh: float,
    end_at: datetime,
    end_kwh: float,
    source_since: datetime | None = None,
) -> tuple[float, str]:
    """The average price a measured print is costed at, and how it was reached.

    The print's counter points are its start reading, the plug's snapshots strictly between the two
    readings -- only those of the print's own measurement source, i.e. strictly after the source's
    cutoff (``meter_source.admits``'s rule for a row that carries no token) -- and its end reading.
    ``cost_of_steps`` costs them; the answer is that cost per kWh, so the caller's own energy figure
    is costed at it whatever a reset inside the window did to the steps. With no point in between
    it is one step at the price in effect at the start; a start whose moment was never recorded
    (an archive from before the plug's source was remembered) takes the price at the end reading.
    One query, and nothing but the database.
    """
    from backend.app.models.smart_plug_energy_snapshot import SmartPlugEnergySnapshot

    end = naive_utc(end_at)
    if start_at is None:
        return price_at(history, end), METHOD_SINGLE
    start = naive_utc(start_at)
    conditions = [
        SmartPlugEnergySnapshot.plug_id == plug_id,
        SmartPlugEnergySnapshot.recorded_at > start,
        SmartPlugEnergySnapshot.recorded_at < end,
    ]
    if source_since is not None:
        conditions.append(SmartPlugEnergySnapshot.recorded_at > naive_utc(source_since))
    between = (
        await db.execute(
            select(SmartPlugEnergySnapshot.recorded_at, SmartPlugEnergySnapshot.lifetime_kwh)
            .where(*conditions)
            .order_by(SmartPlugEnergySnapshot.recorded_at, SmartPlugEnergySnapshot.id)
        )
    ).all()
    points = [(start, float(start_kwh)), *((naive_utc(at), float(kwh)) for at, kwh in between), (end, float(end_kwh))]
    steps = cost_of_steps(points, history)
    return price_of(steps, price_at(history, start)), METHOD_STEPS if between else METHOD_SINGLE


async def stored_price(db) -> float:
    """``energy_cost_per_kwh`` as stored: the price now (the fixed one, or the last one read)."""
    from backend.app.models.settings import Settings

    raw = await db.scalar(select(Settings.value).where(Settings.key == SETTING_KEY))
    return DEFAULT_PRICE if raw is None else clean_price(raw)


async def load_history(db) -> list[PricePoint]:
    """The whole price history in time order -- read once per computation, never per step.

    A database whose table is still empty (it has not met m245's seed) answers with the stored price
    as its baseline, so every moment costs what the price is now -- what it did before the history.
    """
    rows = (
        await db.execute(
            select(EnergyPriceHistory.effective_at, EnergyPriceHistory.price_per_kwh).order_by(
                EnergyPriceHistory.effective_at, EnergyPriceHistory.id
            )
        )
    ).all()
    if not rows:
        return [PricePoint(BASELINE_AT, await stored_price(db))]
    return [PricePoint(naive_utc(at), float(price)) for at, price in rows]


# ── Writing ──────────────────────────────────────────────────────────────────


async def _latest(db):
    return (
        await db.execute(
            select(EnergyPriceHistory.effective_at, EnergyPriceHistory.price_per_kwh)
            .order_by(EnergyPriceHistory.effective_at.desc(), EnergyPriceHistory.id.desc())
            .limit(1)
        )
    ).first()


async def record_change(db, price: float, source: str, at: datetime | None = None) -> bool:
    """Append a change of the price; False when ``price`` is the standing one. Never commits.

    Called in the transaction that changes the setting and BEFORE the setting's row is written: a
    table that is still empty gets its baseline from the price that stands, so the past keeps it.
    A moment at or before the newest row (the clock went back) is placed right after that row -- the
    history stays in the order the changes were made.
    """
    if source not in CHANGE_SOURCES or source == "baseline":
        raise ValueError(f"Not a price change source: {source!r}")
    price = clean_price(price)
    latest = await _latest(db)
    if latest is None:
        standing = await stored_price(db)
        await db.execute(
            insert(EnergyPriceHistory).values(effective_at=BASELINE_AT, price_per_kwh=standing, source="baseline")
        )
        latest = (BASELINE_AT, standing)
    if float(latest[1]) == price:
        return False
    moment = naive_utc(at)
    newest = naive_utc(latest[0])
    if moment <= newest:
        moment = newest + timedelta(microseconds=1)
    await db.execute(insert(EnergyPriceHistory).values(effective_at=moment, price_per_kwh=price, source=source))
    return True


async def note_setting_write(db, raw_value, source: str) -> None:
    """The seam every writer of the ``energy_cost_per_kwh`` row passes BEFORE it writes the row.

    ``raw_value`` is what is about to be stored (None for a row about to be deleted -- the default
    then stands). The settings route's ``set_setting`` calls this for the key by itself; a writer
    that touches the row another way (the reset, the Git restore) calls it explicitly.
    """
    await record_change(db, DEFAULT_PRICE if raw_value is None else clean_price(raw_value), source)


async def set_price(db, price: float, source: str) -> float:
    """Record the change and store ``price`` as the price now. Never commits. Returns what was stored."""
    from backend.app.core.db_dialect import upsert_setting
    from backend.app.models.settings import Settings

    price = clean_price(price)
    await record_change(db, price, source)
    await upsert_setting(db, Settings, SETTING_KEY, str(price))
    return price


# ── The price from Home Assistant ────────────────────────────────────────────


@dataclass(frozen=True)
class PriceTarget:
    """The frozen destination of one price read: a validated connection and the sensor."""

    url: str
    token: str
    entity: str

    def __repr__(self) -> str:  # never a token in a log line or a traceback
        return f"PriceTarget(entity={self.entity!r})"


class PriceReadFailed(Exception):
    """A read that gave no price. ``reason`` is a closed code; nothing here carries a URL or a token."""

    REASONS = ("not_configured", "no_entity", "timeout", "unreachable", "no_such_entity", "unusable")

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def price_from_state(state) -> float:
    """A per-kWh price from a Home Assistant state object; raises ``PriceReadFailed("unusable")``.

    The unit after the "/" scales it (``/MWh`` ÷ 1000, ``/Wh`` × 1000; anything else is per kWh).
    A negative price -- paid out in some markets -- is taken as free.
    """
    if not isinstance(state, dict):
        raise PriceReadFailed("unusable")
    raw = state.get("state")
    if isinstance(raw, bool) or raw is None:
        raise PriceReadFailed("unusable")
    try:
        value = float(raw)  # "unavailable" / "unknown" / "" are not numbers
    except (TypeError, ValueError):
        raise PriceReadFailed("unusable") from None
    attributes = state.get("attributes")
    unit = str((attributes if isinstance(attributes, dict) else {}).get("unit_of_measurement") or "")
    per = unit.rsplit("/", 1)[-1].strip().lower() if "/" in unit else ""
    value *= _UNIT_SCALE.get(per, 1.0)
    if not math.isfinite(value):
        raise PriceReadFailed("unusable")
    return max(0.0, value)


def target_from(ha: dict, entity: str | None) -> PriceTarget:
    """A read's destination from the EFFECTIVE Home Assistant settings; raises when there is none."""
    entity = (entity or "").strip()
    if not ha.get("ha_enabled") or not ha.get("ha_url") or not ha.get("ha_token"):
        raise PriceReadFailed("not_configured")
    if not entity:
        raise PriceReadFailed("no_entity")
    return PriceTarget(url=str(ha["ha_url"]), token=str(ha["ha_token"]), entity=entity)


async def read_target(db) -> PriceTarget | None:
    """The stored destination, or None while the price is fixed. Raises ``PriceReadFailed`` when the
    source is Home Assistant and there is nothing to ask. Reads only; the caller ends the transaction
    before any network I/O."""
    from backend.app.api.routes.settings import get_homeassistant_settings
    from backend.app.models.settings import Settings

    rows = dict(
        (
            await db.execute(
                select(Settings.key, Settings.value).where(Settings.key.in_((SOURCE_SETTING_KEY, ENTITY_SETTING_KEY)))
            )
        ).all()
    )
    if rows.get(SOURCE_SETTING_KEY) != PRICE_SOURCE_HOMEASSISTANT:
        return None
    return target_from(await get_homeassistant_settings(db), rows.get(ENTITY_SETTING_KEY))


async def fetch_price(target: PriceTarget) -> float:
    """Ask Home Assistant for the sensor's state. Network I/O: no transaction is open around this."""
    import httpx

    from backend.app.services.homeassistant import homeassistant_service

    try:
        state = await homeassistant_service.read_state(
            target.url, target.token, target.entity, timeout=HA_TIMEOUT_SECONDS
        )
    except httpx.TimeoutException:
        raise PriceReadFailed("timeout") from None
    except httpx.HTTPStatusError as exc:
        raise PriceReadFailed("no_such_entity" if exc.response.status_code == 404 else "unreachable") from None
    except Exception:  # noqa: BLE001 -- the text of a transport error names the URL
        raise PriceReadFailed("unreachable") from None
    return price_from_state(state)


async def refresh_from_homeassistant(session_factory=None) -> float | None:
    """One scheduled read: the sensor's price becomes the price now, with its change recorded.

    Returns the price written (or confirmed), None when the source is fixed or the read failed -- the
    last price then stands, and one WARNING says why, without a URL or a token. Three steps, in this
    order: freeze the destination in a short transaction; ask the network with no transaction open;
    write in a new short transaction, under the credential anchor and only while the destination is
    still the one that was asked.
    """
    from backend.app.core.credential_config import lock_credential_config

    if session_factory is None:
        from backend.app.core.database import async_session as session_factory

    try:
        async with session_factory() as db:
            target = await read_target(db)
        if target is None:
            return None
        price = await fetch_price(target)
    except PriceReadFailed as exc:
        logger.warning("Electricity price was not read from Home Assistant (%s); the last price stays", exc.reason)
        return None

    async with session_factory() as db:
        await lock_credential_config(db)
        try:
            current = await read_target(db)
        except PriceReadFailed:
            current = None
        if current != target:
            await db.rollback()
            return None  # reconfigured while it was being read: this price is not the new source's
        stored = await set_price(db, price, "homeassistant")
        await db.commit()
    return stored
