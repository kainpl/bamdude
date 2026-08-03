"""Thresholds on sensor readings: the rule, and the two sweeps that apply it.

The rule is a pure function so it can be read in one screen and tested without
a database. Everything below it exists to feed it the newest reading and to
write down what it decided.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from backend.app.models.smart_sensor import SmartSensor
from backend.app.models.smart_sensor_history import SmartSensorHistory
from backend.app.models.smart_sensor_threshold import SmartSensorThreshold
from backend.app.services.notification_service import notification_service
from backend.app.services.zigbee.device_settings import (
    DEFAULT_REPORTING_STALE_MULTIPLIER,
    load_device_row,
    resolve_max_interval,
)
from backend.app.services.zigbee.measurements import BY_KEY

logger = logging.getLogger(__name__)

OK = "ok"
ABOVE = "above"
BELOW = "below"


def next_state(
    current: str,
    value: float,
    *,
    min_value: float | None,
    max_value: float | None,
    deadband: float,
) -> str:
    """What this threshold's state becomes, given a reading.

    The deadband applies **only on the way out**. Applying it on the way in
    would mean a threshold of 30 with a deadband of 1 actually alarms at 31,
    and nothing on any screen would say so.

    A limit that is not set can never be crossed: a threshold carrying only a
    maximum never produces ``below``, whatever the reading.
    """
    if max_value is not None and value > max_value:
        return ABOVE
    if min_value is not None and value < min_value:
        return BELOW

    # Inside both raw limits. Whether an existing alarm clears is the only
    # question left, and it is the only place the deadband is consulted.
    if current == ABOVE:
        if max_value is None or value <= max_value - deadband:
            return OK
        return ABOVE
    if current == BELOW:
        if min_value is None or value >= min_value + deadband:
            return OK
        return BELOW
    return OK


def template_for(previous: str, new: str) -> str | None:
    """Which message a transition is, or None when nothing changed.

    "Above" and "below" are different sentences rather than a variable, because
    the sentence is the translation boundary. There is one all-clear: which
    side it returned from is not news.
    """
    if previous == new:
        return None
    if new == ABOVE:
        return "sensor_above_max"
    if new == BELOW:
        return "sensor_below_min"
    return "sensor_back_in_range"


# When this module was imported, which is within seconds of the loop starting.
# The silence sweep is guarded on it: every sensor's last reading is older than
# a process that just booted, so an unguarded sweep announces silence for the
# whole farm on every restart.
_LOADED_AT = datetime.now(timezone.utc)


def uptime_seconds() -> float:
    return (datetime.now(timezone.utc) - _LOADED_AT).total_seconds()


@dataclass(frozen=True)
class AlertEvent:
    """One thing worth telling somebody. Carries the template key and the
    variables it needs — rendering and sending belong to the notifier."""

    sensor_id: int
    sensor_name: str
    location: str
    template: str
    variables: dict[str, str]


def _place(sensor: SmartSensor) -> str:
    """Where to walk. A sensor with no location falls back to its own name: a
    message opening with an empty dash says nothing."""
    return (sensor.location.path if sensor.location else None) or sensor.name


def _number(value: float) -> str:
    """Trailing zeros make a limit of 30 read as 30.0, which looks like a
    measurement rather than a setting."""
    return f"{value:g}"


async def evaluate_thresholds(db) -> list[AlertEvent]:
    """Apply every enabled threshold to the newest reading it has, and write
    down what changed.

    The input is the newest recorded row, not whatever was just flushed: that
    way the decision does not depend on which path a reading took into the
    database, and it survives a restart between a report and a flush. Reading
    the same unchanged value on every tick is harmless — only a CHANGE of state
    produces an event.

    One query per threshold. There is one row per sensor per quantity, so the
    count is a handful; a window function that had to be written twice for two
    engines would cost more than it saves.
    """
    events: list[AlertEvent] = []
    rows = (await db.execute(select(SmartSensorThreshold))).scalars().all()

    for row in rows:
        if not row.enabled:
            # Switched off: forget any alarm silently. An all-clear about a
            # limit somebody just disabled is a message about nothing.
            if row.state != OK:
                row.state = OK
                row.state_since = datetime.now(timezone.utc)
            continue

        reading = (
            await db.execute(
                select(SmartSensorHistory)
                .where(
                    SmartSensorHistory.sensor_id == row.sensor_id,
                    SmartSensorHistory.sensor_kind == row.kind,
                )
                .order_by(SmartSensorHistory.recorded_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if reading is None:
            continue

        new_state = next_state(
            row.state,
            reading.value,
            min_value=row.min_value,
            max_value=row.max_value,
            deadband=row.deadband or 0.0,
        )
        template = template_for(row.state, new_state)
        if template is None:
            continue

        sensor = await db.get(SmartSensor, row.sensor_id)
        if sensor is None:
            continue

        measurement = BY_KEY.get(row.kind)
        limit = row.max_value if new_state == ABOVE else row.min_value
        events.append(
            AlertEvent(
                sensor_id=sensor.id,
                sensor_name=sensor.name,
                location=_place(sensor),
                template=template,
                variables={
                    "location": _place(sensor),
                    "sensor": sensor.name,
                    "quantity": row.kind,
                    "value": _number(reading.value),
                    "unit": measurement.unit if measurement else "",
                    "limit": _number(limit) if limit is not None else "",
                },
            )
        )

        row.state = new_state
        row.state_since = datetime.now(timezone.utc)
        row.notified_at = datetime.now(timezone.utc)

    # Committed BEFORE anything is sent. The other order turns a sustained
    # database failure into an identical message every minute.
    await db.commit()
    return events


async def _silence_window(db, sensor: SmartSensor) -> int | None:
    """How long this sensor may be quiet before it counts as silent.

    The longest of its own staleness windows, over the quantities it has
    actually recorded. No new number is introduced: that window already exists,
    is derived from what the device promised, and is already overridable per
    device.
    """
    kinds = (
        (
            await db.execute(
                select(SmartSensorHistory.sensor_kind).where(SmartSensorHistory.sensor_id == sensor.id).distinct()
            )
        )
        .scalars()
        .all()
    )
    if not kinds:
        return None

    row = await load_device_row(db, sensor.zigbee_ieee)
    if row is not None and row.stale_after_seconds:
        return int(row.stale_after_seconds)

    windows = []
    for kind in kinds:
        interval = await resolve_max_interval(db, sensor.zigbee_ieee, kind)
        if interval > 0:
            windows.append(interval * DEFAULT_REPORTING_STALE_MULTIPLIER)
    return max(windows) if windows else None


async def sweep_silence(db, *, uptime_seconds: float) -> list[AlertEvent]:
    """Notice sensors that stopped talking, and ones that started again.

    Every adopted sensor, not only those carrying thresholds: adoption is
    itself the deliberate act of caring about a device. Nobody who did not ask
    is troubled, because ``on_sensor_silent`` defaults to off.
    """
    events: list[AlertEvent] = []
    now = datetime.now(timezone.utc)
    sensors = (await db.execute(select(SmartSensor))).scalars().all()

    for sensor in sensors:
        window = await _silence_window(db, sensor)
        if window is None:
            # Never reported. That is "not set up yet", not "went silent".
            continue
        # A process younger than the window cannot tell silence from a restart.
        if uptime_seconds < window:
            continue

        newest = (
            await db.execute(
                select(SmartSensorHistory.recorded_at)
                .where(SmartSensorHistory.sensor_id == sensor.id)
                .order_by(SmartSensorHistory.recorded_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if newest is None:
            continue
        if newest.tzinfo is None:
            # SQLite hands back naive datetimes; everything computed here is
            # aware, and subtracting one from the other raises.
            newest = newest.replace(tzinfo=timezone.utc)

        quiet_for = (now - newest).total_seconds()

        if quiet_for > window and sensor.silent_since is None:
            sensor.silent_since = newest
            sensor.silence_notified_at = now
            events.append(
                AlertEvent(
                    sensor_id=sensor.id,
                    sensor_name=sensor.name,
                    location=_place(sensor),
                    template="sensor_silent",
                    variables={
                        "location": _place(sensor),
                        "sensor": sensor.name,
                        "minutes": str(int(quiet_for // 60)),
                    },
                )
            )
        elif quiet_for <= window and sensor.silent_since is not None:
            sensor.silent_since = None
            sensor.silence_notified_at = now
            events.append(
                AlertEvent(
                    sensor_id=sensor.id,
                    sensor_name=sensor.name,
                    location=_place(sensor),
                    template="sensor_speaking_again",
                    variables={"location": _place(sensor), "sensor": sensor.name},
                )
            )

    await db.commit()
    return events


async def run_sensor_alerts(db) -> int:
    """One tick: decide, then tell. Returns how many alerts were sent.

    Every send is wrapped. This runs inside the loop that also writes
    measurement history and prunes it, and an exception there is a feature that
    silently stops working — taking the other two with it.
    """
    events = await evaluate_thresholds(db)
    events.extend(await sweep_silence(db, uptime_seconds=uptime_seconds()))

    sent = 0
    for event in events:
        try:
            await notification_service.on_sensor_alert(event, db)
            sent += 1
        except Exception as exc:  # noqa: BLE001 — see the docstring
            logger.warning("Sensor alert %s for sensor %s not sent: %s", event.template, event.sensor_id, exc)
    return sent
