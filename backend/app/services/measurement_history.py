"""Flush, sample and prune. The loop's work, written so it can be tested without one.

Three jobs that belong together because they share a schedule:

* **flush** — take what the report handlers buffered and write it;
* **sample** — read the plug types that never report, and buffer that too;
* **prune** — drop what is older than its retention window, once a day.

Sampling exists because three of the five plug types have no reports at all.
Their only readings today come from the frontend polling a status endpoint,
which would make history a record of when somebody had a tab open: empty
overnight, denser with two browsers. The loop reads them instead.

Pruning lives here rather than beside the writers for a duller reason: Zigbee
and MQTT rows are written from *callbacks*, and a callback has nowhere to keep
"once a day". This is the only place in the subsystem with a schedule.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from backend.app.api.routes.settings import get_setting
from backend.app.models.smart_plug import SmartPlug
from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory
from backend.app.models.smart_sensor import SmartSensor
from backend.app.models.smart_sensor_history import SmartSensorHistory
from backend.app.services.measurement_buffer import measurement_buffer
from backend.app.services.smart_plug_manager import smart_plug_manager

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30
DEFAULT_SAMPLE_SECONDS = 60

# Types whose devices report on their own. Everything else has to be asked.
# Sampling a reporting plug as well would double its rows and spend the radio
# for a reading that is already on its way.
REPORTING_PLUG_TYPES = frozenset({"zigbee", "mqtt"})


async def _setting_int(db, key: str, default: int) -> int:
    """Total by construction: this is read from a background loop, where an
    exception is a feature that quietly stops working."""
    try:
        value = int(await get_setting(db, key))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


async def resolve_sample_seconds(db) -> int:
    return await _setting_int(db, "plug_power_sample_seconds", DEFAULT_SAMPLE_SECONDS)


async def flush_buffered(db) -> int:
    """Write everything the callbacks left. Returns how many rows landed.

    A reading whose device has gone — a plug deleted between the report and the
    flush, a sensor never adopted — is dropped rather than raised. The loop must
    survive a device disappearing; one lost row is not worth a stopped flush.
    """
    power, sensors = measurement_buffer.drain()
    if not power and not sensors:
        return 0

    written = 0

    if power:
        known = {
            plug_id
            for (plug_id,) in await db.execute(select(SmartPlug.id).where(SmartPlug.id.in_({s.plug_id for s in power})))
        }
        for sample in power:
            if sample.plug_id not in known:
                continue
            db.add(SmartPlugPowerHistory(plug_id=sample.plug_id, power=sample.power, recorded_at=sample.recorded_at))
            written += 1

    if sensors:
        # The buffer carries an IEEE because that is what a report knows; the
        # table keys on the adopted row, so the two are joined here and nowhere
        # else. An unadopted sensor simply has no row to point at.
        by_ieee = {
            str(ieee).lower(): sensor_id
            for sensor_id, ieee in await db.execute(select(SmartSensor.id, SmartSensor.zigbee_ieee))
        }
        for sample in sensors:
            sensor_id = by_ieee.get(sample.ieee.strip().lower())
            if sensor_id is None:
                continue
            db.add(
                SmartSensorHistory(
                    sensor_id=sensor_id,
                    sensor_kind=sample.kind,
                    value=sample.value,
                    recorded_at=sample.recorded_at,
                )
            )
            written += 1

    await db.commit()
    return written


async def sample_polled_plugs(db) -> int:
    """Read the plugs that never report, and buffer what they say.

    Goes through ``get_service_for_plug`` — the one resolver — so a plug type
    added later is covered without touching this. Best-effort per plug: one
    unreachable device must not cost the others their history.
    """
    plugs = (await db.execute(select(SmartPlug).where(SmartPlug.enabled.is_(True)))).scalars().all()
    sampled = 0
    for plug in plugs:
        if (plug.plug_type or "").strip().lower() in REPORTING_PLUG_TYPES:
            continue
        try:
            service = await smart_plug_manager.get_service_for_plug(plug, db)
            energy = await service.get_energy(plug)
        except Exception as exc:  # noqa: BLE001 — one plug must not lose the rest
            logger.debug("History: could not read plug %s: %s", plug.id, exc)
            continue
        power = (energy or {}).get("power")
        if power is None:
            # No reading is not zero watts. A fabricated zero would be drawn as
            # "the printer was off", which is a different claim entirely.
            continue
        measurement_buffer.record_power(plug.id, power)
        sampled += 1
    return sampled


async def prune(db) -> tuple[int, int]:
    """Drop what is past its window. Returns (power rows, sensor rows) removed."""
    now = datetime.now(timezone.utc)
    power_days = await _setting_int(db, "plug_power_history_retention_days", DEFAULT_RETENTION_DAYS)
    sensor_days = await _setting_int(db, "sensor_history_retention_days", DEFAULT_RETENTION_DAYS)

    power_removed = (
        await db.execute(
            delete(SmartPlugPowerHistory).where(SmartPlugPowerHistory.recorded_at < now - timedelta(days=power_days))
        )
    ).rowcount or 0
    sensor_removed = (
        await db.execute(
            delete(SmartSensorHistory).where(SmartSensorHistory.recorded_at < now - timedelta(days=sensor_days))
        )
    ).rowcount or 0
    await db.commit()
    if power_removed or sensor_removed:
        logger.info("History pruned: %d power row(s), %d sensor row(s)", power_removed, sensor_removed)
    return power_removed, sensor_removed
