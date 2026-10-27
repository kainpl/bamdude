"""Measurement sources of smart plugs (spec plug-meter-watchdog W2, W3, W4).

A plug row says what device and which numbers a reading comes from. When that changes -- another IP,
topic, path, multiplier, entity, IEEE, plug type, phantom policy or printer -- the readings that follow
are of another source, even under the same plug id. One random token per configuration
(``measurement_source_id``) and a strict cutoff (``measurement_source_since``) keep them apart without
a source column in the big history tables:

* producers capture the token before their I/O (a sampler) or when they are set up (a listener);
* the flush and the counter-snapshot writers drop a fact whose token is not the plug's current one,
  under ``source_guard`` -- the same lock a rotation holds, so a check and its insert are one step
  relative to a configuration change;
* readers take only rows strictly after the cutoff. The cutoff is max(now, the previous cutoff, the
  newest row of the plug), so a row of the same instant or one written before a clock went back
  never becomes the new source's.

A rename, a reconnect or a restart rotates nothing. Restoring a backup brings its own tokens back --
the same token is the same configuration; what restore must stop are the old storage's writers.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, update

#: The fields whose change makes the readings another source's. Credentials are not among them:
#: a new password reads the same meter.
IDENTITY_FIELDS: tuple[str, ...] = (
    "plug_type",
    "ip_address",
    "ha_entity_id",
    "ha_power_entity",
    "ha_energy_today_entity",
    "ha_energy_total_entity",
    "mqtt_topic",
    "mqtt_power_topic",
    "mqtt_power_path",
    "mqtt_power_multiplier",
    "mqtt_energy_topic",
    "mqtt_energy_path",
    "mqtt_energy_multiplier",
    "mqtt_state_topic",
    "mqtt_state_path",
    "mqtt_state_on_value",
    "mqtt_energy_total_topic",
    "mqtt_energy_total_path",
    "mqtt_energy_total_multiplier",
    "mqtt_multiplier",
    "rest_status_url",
    "rest_status_path",
    "rest_status_on_value",
    "rest_power_url",
    "rest_power_path",
    "rest_power_multiplier",
    "rest_energy_url",
    "rest_energy_path",
    "rest_energy_multiplier",
    "rest_energy_total_path",
    "rest_energy_total_multiplier",
    "zigbee_ieee",
    "printer_id",
    "phantom_watts_risk",
)

#: The Zigbee poller reads every plug every 30-45 s, so a Zigbee history is never sparser than that.
ZIGBEE_CADENCE_SECONDS = 45
#: An MQTT plug's expected publish interval when nobody set one (Tasmota's TelePeriod default).
DEFAULT_MQTT_CADENCE_SECONDS = 300
#: The smallest gap limit any source gets (W4).
MIN_GAP_SECONDS = 180
#: The default energy a flat counter must have missed before it is suspected (Wh).
DEFAULT_MIN_EXPECTED_WH = 100.0

_guards: dict[int, asyncio.Lock] = {}


def source_guard() -> asyncio.Lock:
    """The lock that makes "check the token, then write" one step against a rotation (W2).

    One per event loop: the tests run several, and an asyncio.Lock belongs to the loop it is used on.
    Held only around the check-and-commit of a flush or a snapshot and around a rotation -- never
    across device I/O.
    """
    loop = asyncio.get_running_loop()
    lock = _guards.get(id(loop))
    if lock is None:
        lock = _guards[id(loop)] = asyncio.Lock()
    return lock


def naive_utc(moment: datetime | None = None) -> datetime:
    """UTC without a zone -- what the history and snapshot columns hold."""
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def identity_of(plug) -> tuple:
    return tuple(getattr(plug, field, None) for field in IDENTITY_FIELDS)


def new_token() -> str:
    return uuid.uuid4().hex


def start_source(plug, now: datetime | None = None) -> None:
    """A new plug's first source: nothing came before it, so the cutoff is now."""
    plug.measurement_source_id = new_token()
    plug.measurement_source_since = naive_utc(now)
    plug.meter_watch_cursor = None


async def rotate_source(db, plug, now: datetime | None = None) -> str:
    """Give ``plug`` a new source token and a strict cutoff (W2). The caller holds ``source_guard``.

    Closes an active stall episode as ``source_changed`` -- never as recovered. Returns the token.
    """
    from backend.app.models.smart_plug_energy_snapshot import SmartPlugEnergySnapshot
    from backend.app.models.smart_plug_meter_incident import SmartPlugMeterIncident
    from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory

    current = naive_utc(now)
    newest_power = await db.scalar(
        select(func.max(SmartPlugPowerHistory.recorded_at)).where(SmartPlugPowerHistory.plug_id == plug.id)
    )
    newest_snapshot = await db.scalar(
        select(func.max(SmartPlugEnergySnapshot.recorded_at)).where(SmartPlugEnergySnapshot.plug_id == plug.id)
    )
    candidates = [current, plug.measurement_source_since, newest_power, newest_snapshot]
    cutoff = max(naive_utc(c) if c.tzinfo else c for c in candidates if c is not None)
    plug.measurement_source_id = new_token()
    plug.measurement_source_since = cutoff
    plug.meter_watch_cursor = None
    await db.execute(
        update(SmartPlugMeterIncident)
        .where(SmartPlugMeterIncident.plug_id == plug.id, SmartPlugMeterIncident.status == "active")
        .values(status="closed", closed_at=current, close_reason="source_changed")
    )
    return plug.measurement_source_id


def phantom_risk(plug) -> bool:
    """Whether an unknown relay state makes this plug's watts unusable (W3, O14).

    Zigbee by default -- the firmware that started this repeats its last watts after the relay opens.
    Every other type reports what it measures; a plug can be marked otherwise either way.
    """
    explicit = getattr(plug, "phantom_watts_risk", None)
    if explicit is not None:
        return bool(explicit)
    return (getattr(plug, "plug_type", None) or "").strip().lower() == "zigbee"


def min_expected_wh(plug) -> float:
    value = plug.meter_min_expected_wh
    return max(DEFAULT_MIN_EXPECTED_WH, float(value)) if value else DEFAULT_MIN_EXPECTED_WH


@dataclass(frozen=True)
class Cadence:
    """How often a source's history is expected to have a point, and the limits that follow (W4)."""

    expected_seconds: int
    gap_seconds: int
    edge_seconds: int


def cadence_of(plug, sample_seconds: int) -> Cadence:
    kind = (plug.plug_type or "").strip().lower()
    if kind == "zigbee":
        return Cadence(ZIGBEE_CADENCE_SECONDS, MIN_GAP_SECONDS, MIN_GAP_SECONDS)
    if kind == "mqtt":
        expected = plug.mqtt_expected_power_interval_seconds or DEFAULT_MQTT_CADENCE_SECONDS
    else:
        expected = sample_seconds
    gap = max(MIN_GAP_SECONDS, 3 * int(expected))
    return Cadence(int(expected), gap, gap)
