"""Per-device settings: three layers, and the row they live in.

**registry defaults → global setting → this device's row.**

The middle layer is the existing ``zigbee_sensor_reporting`` key, generalised:
it is *defaults for devices that have no override of their own*, which is what
it was asked for. Changing it must not reach into a device somebody configured
deliberately.

Every loader is total. A corrupt or nonsensical value at any layer falls back to
the layer beneath rather than raising: these are read from a pairing callback
and from a background loop, where an exception is a feature that silently stops
working.

Reporting parameters live IN the device — editing a setting here does nothing
until ``configure_reporting`` is re-issued. The poll and staleness values are
the opposite: they are ours, and take effect on the next cycle of the loop that
reads them.
"""

from __future__ import annotations

import json
import logging

from backend.app.api.routes.settings import get_setting
from backend.app.models.zigbee_device import ZigbeeDevice
from backend.app.services.zigbee.devices import DeviceInfo, DeviceKind
from backend.app.services.zigbee.reporting_targets import ReportingTarget, targets_for

logger = logging.getLogger(__name__)

GLOBAL_REPORTING_KEY = "zigbee_sensor_reporting"
GLOBAL_POLL_KEY = "zigbee_sensor_poll_seconds"

DEFAULT_POLL_SECONDS = 30
# What ``driver.py`` has always used for a polled device. Kept as a number
# rather than derived from the poll interval: deriving it shortens the time to
# "unreachable", and a plug wrongly marked offline is worse than one marked
# late, because that is the reading people act on.
DEFAULT_POLLED_STALE_SECONDS = 120
DEFAULT_REPORTING_STALE_MULTIPLIER = 2

_INTEGER_FIELDS = ("min_interval", "max_interval")
_FIELDS = ("min_interval", "max_interval", "reportable_change")


def _base(targets: tuple[ReportingTarget, ...]) -> dict[str, dict]:
    return {
        t.key: {
            "min_interval": t.min_interval,
            "max_interval": t.max_interval,
            "reportable_change": t.reportable_change,
        }
        for t in targets
    }


def _overlay(into: dict[str, dict], layer) -> None:
    """Apply one layer in place, field by field, ignoring anything unusable.

    Field by field rather than key by key on purpose: a layer that sets only
    ``max_interval`` must leave the other two to the layer beneath instead of
    blanking them. A key this device has no target for is dropped — the layer
    may predate a registry change, or the IEEE may now carry another model.
    """
    if not isinstance(layer, dict):
        return
    for key, values in layer.items():
        if key not in into or not isinstance(values, dict):
            continue
        for field in _FIELDS:
            if field not in values:
                continue
            try:
                into[key][field] = int(values[field]) if field in _INTEGER_FIELDS else float(values[field])
            except (TypeError, ValueError):
                logger.warning("Zigbee settings: %s.%s is not a number — keeping the value beneath", key, field)


async def load_device_row(db, ieee: str) -> ZigbeeDevice | None:
    """This device's row, or None — including when the row cannot be read.

    Total like the rest of this module, and for a sharper reason than the other
    loaders. The callers are a pairing callback and a background loop, and the
    one above them swallows exceptions at debug level: a database hiccup here
    would not surface as an error, it would surface as a sensor that quietly
    stops being reconfigured until the next restart. Losing the per-device layer
    and applying the two beneath it is strictly better than applying nothing,
    and the warning says which happened.
    """
    try:
        return await db.get(ZigbeeDevice, str(ieee).strip().lower())
    except Exception as exc:  # noqa: BLE001 — see the docstring
        logger.warning("Zigbee %s: could not read device settings, using the farm defaults: %s", ieee, exc)
        return None


async def resolve_reporting(db, info: DeviceInfo) -> dict[str, dict]:
    """The parameters this device should be running, all three layers applied."""
    resolved = _base(targets_for(info))
    if not resolved:
        return resolved

    raw = await get_setting(db, GLOBAL_REPORTING_KEY)
    if raw:
        try:
            _overlay(resolved, json.loads(raw))
        except (ValueError, TypeError):
            logger.warning("%s is not valid JSON — using the layer beneath", GLOBAL_REPORTING_KEY)

    row = await load_device_row(db, info.ieee)
    if row is not None:
        _overlay(resolved, row.reporting)
    return resolved


async def resolve_poll_seconds(db, ieee: str) -> int:
    row = await load_device_row(db, ieee)
    if row is not None and row.poll_seconds:
        return int(row.poll_seconds)
    raw = await get_setting(db, GLOBAL_POLL_KEY)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_POLL_SECONDS
    return value if value > 0 else DEFAULT_POLL_SECONDS


async def resolve_stale_after_seconds(db, ieee: str, *, polled: bool, max_interval: int) -> int:
    """After how many seconds we stop trusting the last value.

    One question, one absolute answer — but the default comes from whichever
    mechanism actually keeps this device fresh. A polled device is judged
    against the poll; a sleeper against its own reporting interval, so that
    telling it to speak less often moves the threshold with it instead of making
    the new interval read as a fault.
    """
    row = await load_device_row(db, ieee)
    if row is not None and row.stale_after_seconds:
        return int(row.stale_after_seconds)
    if polled:
        return DEFAULT_POLLED_STALE_SECONDS
    return int(max_interval) * DEFAULT_REPORTING_STALE_MULTIPLIER


async def upsert_device_row(db, info: DeviceInfo) -> ZigbeeDevice:
    """Create the row when a device pairs, or refresh what the radio now says.

    Never touches the operator's columns. Re-pairing a device that walked out of
    range must not silently reset what somebody configured on it.
    """
    ieee = str(info.ieee).strip().lower()
    row = await db.get(ZigbeeDevice, ieee)
    hardware_name = " ".join(part for part in (info.manufacturer, info.model) if part) or None
    if row is None:
        row = ZigbeeDevice(ieee=ieee, kind=info.kind.value, name=hardware_name)
        db.add(row)
    else:
        row.kind = info.kind.value
        row.name = hardware_name or row.name
    await db.commit()
    return row


async def reconcile_device_rows(infos, db) -> int:
    """Give every paired device a row, for the ones that predate the table.

    Runs at startup rather than in the migration, which has no radio and cannot
    know what is paired. Idempotent by design and by necessity: it runs on every
    boot, so refreshing an existing row here would wipe an operator's settings
    once per restart with nothing to report it.

    The coordinator and unsupported devices are skipped — one is our own radio,
    the other is something BamDude can neither switch nor read, so there is
    nothing about either that anybody configures.
    """
    added = 0
    for info in infos:
        if info.kind not in (DeviceKind.PLUG, DeviceKind.SENSOR):
            continue
        if await load_device_row(db, info.ieee) is None:
            await upsert_device_row(db, info)
            added += 1
    return added


async def forget_device_row(db, ieee: str) -> None:
    """Drop a device that has left the network, and what the farm did with it.

    The adopted sensor goes too. Keeping it would leave a row claiming the farm
    tracks a device that is no longer reachable and cannot be reached again
    without pairing — at which point the row would be in the way rather than
    useful.
    """
    from sqlalchemy import delete

    from backend.app.models.smart_sensor import SmartSensor

    key = str(ieee).strip().lower()
    await db.execute(delete(SmartSensor).where(SmartSensor.zigbee_ieee == key))
    row = await db.get(ZigbeeDevice, key)
    if row is not None:
        await db.delete(row)
    await db.commit()


async def save_overrides(
    db,
    ieee: str,
    *,
    reporting: dict | None = None,
    poll_seconds: int | None = None,
    stale_after_seconds: int | None = None,
) -> bool:
    """Store what an operator chose. False when there is no such device.

    An empty value clears the override rather than storing emptiness: "no
    override" and "overridden with nothing" would otherwise be the same row and
    mean different things.
    """
    row = await load_device_row(db, ieee)
    if row is None:
        return False
    if reporting is not None:
        row.reporting = reporting or None
    if poll_seconds is not None:
        row.poll_seconds = poll_seconds or None
    if stale_after_seconds is not None:
        row.stale_after_seconds = stale_after_seconds or None
    await db.commit()
    return True
