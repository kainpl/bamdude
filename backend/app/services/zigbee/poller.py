"""Periodic reads that keep the plug cache honest.

Attribute reporting is set up for every plug (see ``reporting.py``) and is
welcome when it works. This module exists because it frequently does not, and
because the failure is invisible: a subscription that silently never fires looks
exactly like a plug whose readings have not changed.

The cadence is ZHA's. Its ``ElectricalMeasurementPoller`` reads the cluster
every 30–45 s for **every** device, and a sibling class exempts precisely four
models known to report reliably — the plug this was built against is not among
them. When the reference implementation for this stack polls by default and
allowlists the exceptions, "reporting is enough" is not a defensible position.

The interval is randomised per cycle for the reason ZHA randomises it: a farm's
worth of plugs polled in lockstep is a burst of mesh traffic every N seconds
instead of a trickle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random

from sqlalchemy import select

from backend.app.models.smart_plug import SmartPlug
from backend.app.services.zigbee.coordinator import zigbee_coordinator
from backend.app.services.zigbee.device_settings import (
    resolve_poll_seconds,
    resolve_reporting,
    resolve_stale_after_seconds,
)
from backend.app.services.zigbee.devices import DeviceKind, describe_device
from backend.app.services.zigbee.measurements import BY_KEY
from backend.app.services.zigbee.reporting import reapply_if_settings_changed
from backend.app.services.zigbee.sensors import PowerClass, power_class, sensor_store

logger = logging.getLogger(__name__)

# A read of a sleeping device is answered out of its parent's buffer or not at
# all; waiting longer than this only holds the shared radio for nothing.
_SENSOR_READ_BUDGET_SECONDS = 8.0

# ZHA's AggregatedClusterPoller._REFRESH_INTERVAL.
_POLL_INTERVAL_SECONDS = (30, 45)


def _cluster_on(device, cluster_id: int):
    """A cluster from any endpoint. Endpoint numbering is not guaranteed."""
    for endpoint in (getattr(device, "endpoints", None) or {}).values():
        cluster = (getattr(endpoint, "in_clusters", None) or {}).get(cluster_id)
        if cluster is not None:
            return cluster
    return None


async def read_sensor_once(device, ieee: str, keys: tuple[str, ...]) -> bool:
    """One bounded attempt to read a sensor's registered attributes.

    Returns whether anything was learned. A failure is not an error: a healthy
    battery sensor is asleep almost always, and its parent holds a request for
    only a few seconds.
    """
    learned = False
    for key in keys:
        measurement = BY_KEY.get(key)
        if measurement is None:
            continue
        cluster = _cluster_on(device, measurement.cluster)
        if cluster is None:
            continue
        try:
            await asyncio.wait_for(
                cluster.read_attributes([measurement.attribute], allow_cache=False, only_cache=False),
                timeout=_SENSOR_READ_BUDGET_SECONDS,
            )
        except Exception:  # noqa: BLE001 — a sleeping device is the normal case
            logger.debug("Zigbee sensor %s: read of %s did not answer in time", ieee, key)

        # The cache is read whether or not the request timed out, and this is
        # the whole point rather than belt and braces. zigpy suppresses the
        # update event for the attribute being READ, so a late answer from a
        # sleeping device lands here and fires no listener — give up on the
        # timeout without looking and zigpy ends up holding a temperature this
        # store has never heard of, which is exactly what happened in the field.
        #
        # It also restores this cache from its database at startup, so taking
        # it is what gives a battery sensor its last known values immediately
        # after a restart instead of a blank hour.
        #
        # Never from what read_attributes returned: that is the raw pre-quirk
        # value. Existing invariant of this subsystem.
        value = cluster.get(measurement.attribute)
        if value is None:
            continue
        sensor_store.record(ieee, key, value)
        learned = True
    return learned


class ZigbeePoller:
    """One background task reading every Zigbee plug on a timer."""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._startup_task: asyncio.Task | None = None

    def start(self, service, session_factory) -> None:
        """Begin polling. Idempotent — a second call is ignored."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(service, session_factory), name="zigbee_poller")
        # One forced pass now rather than after the first sleep: the cluster
        # cache lives in memory, so a restart knows nothing, and a battery
        # sensor would otherwise show no reading until its max_interval elapsed
        # — up to half an hour of a blank endpoint.
        self._startup_task = asyncio.create_task(self._startup_sensor_read(session_factory), name="zigbee_sensor_boot")

    async def stop(self) -> None:
        """Cancel and await the tasks, so shutdown does not race a read."""
        for attribute in ("_startup_task", "_task"):
            task = getattr(self, attribute, None)
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            setattr(self, attribute, None)

    async def _startup_sensor_read(self, session_factory) -> None:
        """One forced read per sensor at startup.

        For a mains sensor this fills the values immediately. For a battery one
        it usually fails, which is expected rather than an error — the device is
        asleep and its parent only holds the request for a few seconds. The
        watchdog then picks it up.
        """
        app = zigbee_coordinator.app
        if app is None:
            return
        async with session_factory() as db:
            try:
                await self._poll_sensors_once(app, db)
            except Exception as exc:  # noqa: BLE001 — startup must never raise
                logger.debug("Zigbee startup sensor read failed: %s", exc)

    async def _run(self, service, session_factory) -> None:
        while True:
            await asyncio.sleep(random.randint(*_POLL_INTERVAL_SECONDS))
            try:
                await self._poll_once(service, session_factory)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a bad cycle must not end the loop
                # Losing the loop would take readings back to "only updates on
                # restart", which is the exact symptom this was written to fix.
                logger.warning("Zigbee poll cycle failed: %s", exc)

    async def _poll_once(self, service, session_factory) -> None:
        """Read every configured Zigbee plug.

        The plug list is re-queried each cycle rather than captured at startup,
        so a plug added afterwards joins the rotation without a restart.
        """
        async with session_factory() as db:
            plugs = (await db.execute(select(SmartPlug).where(SmartPlug.plug_type == "zigbee"))).scalars().all()
        for plug in plugs:
            try:
                await service.refresh(plug)
            except Exception as exc:  # noqa: BLE001 — one plug must not cost the rest their poll
                logger.debug("Zigbee plug %s: poll failed: %s", plug.id, exc)

        # Sensors ride the same cycle but not the same rule, and they must never
        # cost the plugs their poll — hence the separate guard.
        app = zigbee_coordinator.app
        if app is not None:
            async with session_factory() as db:
                try:
                    await self._poll_sensors_once(app, db)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Zigbee sensor cycle failed: %s", exc)

    async def _poll_sensors_once(self, app, db) -> None:
        """Read the sensors that are due.

        Mains sensors are polled on their own cadence; battery sensors are read
        only when they have gone quiet for a whole window, and then just once.
        The choice is per device, from the node descriptor — see
        ``sensors.power_class`` for why it cannot be per class.
        """
        for device in list(getattr(app, "devices", {}).values()):
            info = describe_device(device)
            if info.kind is not DeviceKind.SENSOR:
                continue
            keys = info.measurements
            if not keys:
                continue

            if power_class(device) is PowerClass.MAINS:
                # This method runs on every plug cycle (30–45 s), so the
                # sensor's own cadence has to be checked here or the setting
                # would be decorative.
                window = await resolve_poll_seconds(db, info.ieee)
            else:
                # Judged against how often this device was told to speak, so
                # telling it to speak less often moves the threshold with it
                # instead of turning the new interval into a fault.
                parameters = await resolve_reporting(db, info)
                slowest = max((parameters.get(key, {}).get("max_interval", 1800) for key in keys), default=1800)
                window = await resolve_stale_after_seconds(db, info.ieee, polled=False, max_interval=slowest)

            due = [key for key in keys if sensor_store.due_for_watchdog(info.ieee, key, window=window)]
            if not due:
                continue

            if await read_sensor_once(device, info.ieee, tuple(due)):
                sensor_store.note_success(info.ieee)
                # It answered, so it is awake — the other moment an operator's
                # edit can reach it. Same function the report listener uses.
                await reapply_if_settings_changed(device, info.ieee, keys)
            else:
                sensor_store.note_attempt(info.ieee)


zigbee_poller = ZigbeePoller()
