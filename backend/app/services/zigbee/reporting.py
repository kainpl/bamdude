"""Binding a plug's clusters and routing their reports into the driver cache.

Reporting rather than polling: the device pushes changes, so a farm's worth of
plugs costs no airtime while nothing is happening. ``bind`` tells the device
where to send reports; ``configure_reporting`` says which attributes and how
often.

**One listener per cluster, never one per device.** ``OnOff.on_off`` and
``Metering.current_summ_delivered`` are *both* attribute id ``0x0000``, so a
handler that dispatched on the attribute id alone would file a lifetime energy
counter as the plug's on/off state — the plug would report being "on" with the
value 12345, and nothing would look broken.

Scaling happens here, before the value reaches the cache, so the cache holds
kWh and watts rather than device-specific counts. Pushing raw values inward
would move the scaling problem into every reader instead of solving it once.
"""

from __future__ import annotations

import logging

from zigpy.zcl import foundation

from backend.app.services.zigbee.devices import ELECTRICAL_MEASUREMENT, METERING, ON_OFF
from backend.app.services.zigbee.metering import (
    ENERGY_DIVISOR,
    ENERGY_MULTIPLIER,
    POWER_DIVISOR,
    POWER_MULTIPLIER,
    scale,
)

logger = logging.getLogger(__name__)

# Attribute ids. ON_OFF and SUMMATION collide at 0x0000 by design of the ZCL —
# see the module docstring for why that forces per-cluster listeners.
ATTR_ON_OFF = 0x0000
ATTR_SUMMATION = 0x0000
ATTR_ACTIVE_POWER = 0x050B

# Reporting bounds. The minimum keeps a chattering device from flooding the
# mesh; the maximum is a heartbeat, so a plug that never changes still proves it
# is alive rather than looking unreachable forever.
_MIN_INTERVAL = 5
_MAX_INTERVAL = 300
_REPORTABLE_CHANGE = 1


class ClusterReportListener:
    """Routes one cluster's attribute reports into the driver cache.

    Holds the scaling pair it was created with rather than reading it per
    report: multiplier and divisor are device constants, and fetching them on
    every report would put a mesh round-trip behind a number that never changes.
    """

    def __init__(self, service, plug_id: int, cluster_id: int, multiplier=None, divisor=None):
        self._service = service
        self._plug_id = plug_id
        self._cluster_id = cluster_id
        self._multiplier = multiplier
        self._divisor = divisor

    def attribute_updated(self, attrid, value, timestamp=None) -> None:
        """zigpy calls this synchronously — plain ``def``, never ``async def``.

        An ``async def`` would return a coroutine nobody runs and every report
        would vanish without a trace. zigpy also swallows exceptions from
        listeners at DEBUG level, so the guard below exists to make failures
        visible rather than to protect zigpy.
        """
        try:
            if self._cluster_id == ON_OFF and attrid == ATTR_ON_OFF:
                self._service.update(self._plug_id, state="ON" if value else "OFF")
                return

            if self._cluster_id == METERING and attrid == ATTR_SUMMATION:
                total = scale(value, self._multiplier, self._divisor)
                if total is None:
                    logger.warning(
                        "Zigbee plug %s: energy report unusable (raw=%r, divisor=%r) — not cached",
                        self._plug_id,
                        value,
                        self._divisor,
                    )
                    return
                self._service.update(self._plug_id, energy_total=total)
                return

            if self._cluster_id == ELECTRICAL_MEASUREMENT and attrid == ATTR_ACTIVE_POWER:
                power = scale(value, self._multiplier, self._divisor)
                if power is not None:
                    self._service.update(self._plug_id, power=power)
                return
            # Anything else is noise: devices report far more than we asked for.
        except Exception as exc:  # noqa: BLE001 — see the docstring
            logger.warning("Zigbee plug %s: report for 0x%04X ignored: %s", self._plug_id, attrid, exc)


async def _scaling_pair(cluster, multiplier_attr: str, divisor_attr: str):
    """Read a cluster's multiplier/divisor once.

    Failure returns ``(None, None)``, which downstream turns into "no reading"
    rather than a guess — a device that will not say what its counter means has
    not told us anything.
    """
    try:
        result = await cluster.read_attributes([multiplier_attr, divisor_attr])
        values = result[0] if isinstance(result, (list, tuple)) else result
        return values.get(multiplier_attr), values.get(divisor_attr)
    except Exception as exc:  # noqa: BLE001 — an unreadable scale is not fatal
        logger.warning("Zigbee: could not read %s/%s: %s", multiplier_attr, divisor_attr, exc)
        return None, None


async def bind_plug(service, plug, device) -> dict[int, bool]:
    """Bind and subscribe every cluster this plug actually has.

    Best-effort per cluster: a plug whose Metering refuses to bind should still
    switch. Returns which clusters were wired so the caller can log a plug that
    will never report energy, instead of leaving it to be discovered as a
    permanent absence of readings.
    """
    wired: dict[int, bool] = {}

    for cluster_id, mult_attr, div_attr, attr in (
        (ON_OFF, None, None, ATTR_ON_OFF),
        (METERING, ENERGY_MULTIPLIER, ENERGY_DIVISOR, ATTR_SUMMATION),
        (ELECTRICAL_MEASUREMENT, POWER_MULTIPLIER, POWER_DIVISOR, ATTR_ACTIVE_POWER),
    ):
        cluster = service._cluster(device, cluster_id)
        if cluster is None:
            wired[cluster_id] = False
            continue

        multiplier = divisor = None
        if mult_attr:
            multiplier, divisor = await _scaling_pair(cluster, mult_attr, div_attr)

        listener = ClusterReportListener(
            service=service,
            plug_id=plug.id,
            cluster_id=cluster_id,
            multiplier=multiplier,
            divisor=divisor,
        )
        try:
            cluster.add_listener(listener)
            await cluster.bind()
            result = await cluster.configure_reporting(attr, _MIN_INTERVAL, _MAX_INTERVAL, _REPORTABLE_CHANGE)
            _warn_if_reporting_refused(plug.id, cluster_id, result)
            wired[cluster_id] = True
        except Exception as exc:  # noqa: BLE001 — one cluster failing must not lose the others
            logger.warning("Zigbee plug %s: could not subscribe cluster 0x%04X: %s", plug.id, cluster_id, exc)
            wired[cluster_id] = False
            continue

        # Reporting is about CHANGES; it says nothing about the state right now.
        # Without this read a freshly bound plug stays unknown until it happens
        # to change or the max interval elapses — which on hardware looked
        # exactly like a broken driver: "reporting set up for 1/1 plug(s)" in the
        # log, and status still {state: null, reachable: false}.
        await _seed_from_read(cluster, listener, attr)

    if not wired.get(METERING):
        logger.info(
            "Zigbee plug %s reports no energy — it can be switched, but per-print energy will stay empty",
            plug.id,
        )
    return wired


async def subscribe_all(service, plugs) -> int:
    """Bind and subscribe every Zigbee plug that is on the mesh.

    This is the step whose absence made the driver look configured while doing
    half its job: commands go straight to the cluster and worked, so the plug
    switched on — but nothing fed the cache, so status stayed
    ``{state: null, reachable: false}`` and energy never arrived at all. Found
    on hardware, because every unit test had populated the cache by hand.

    Best-effort per plug, deliberately: one plug that is unplugged or refuses to
    bind must not cost the others their reporting. Returns how many were wired
    so the caller can log it rather than leave a silent partial result.
    """
    wired = 0
    for plug in plugs:
        device = service._device_for(plug)
        if device is None:
            logger.info("Zigbee plug %s: device not on the mesh, no reporting set up", plug.id)
            continue
        try:
            await bind_plug(service, plug, device)
            wired += 1
        except Exception as exc:  # noqa: BLE001 — one plug must not lose the rest
            logger.warning("Zigbee plug %s: could not set up reporting: %s", plug.id, exc)
    return wired


def _warn_if_reporting_refused(plug_id: int, cluster_id: int, result) -> None:
    """``configure_reporting`` answers per attribute; it does not raise on refusal.

    Treating "no exception" as success is how a device that declined the
    subscription looks identical to one that accepted it — and the only symptom
    is readings that never arrive.
    """
    try:
        records = result[0] if isinstance(result, (list, tuple)) else result
        for record in records or []:
            status = getattr(record, "status", None)
            if status is not None and status != foundation.Status.SUCCESS:
                logger.warning(
                    "Zigbee plug %s: device refused reporting on cluster 0x%04X (status=%s)",
                    plug_id,
                    cluster_id,
                    status,
                )
    except Exception:  # noqa: BLE001 — a shape we did not expect is not a failure
        return


async def _seed_from_read(cluster, listener: ClusterReportListener, attr: int) -> None:
    """Read the attribute once and feed it through the same listener.

    Routed through the listener rather than written to the cache directly so a
    seeded value and a reported one go through identical scaling and mapping —
    two paths into one cache is how they come to disagree.
    """
    try:
        result = await cluster.read_attributes([attr])
        values = result[0] if isinstance(result, (list, tuple)) else result
        if attr in (values or {}):
            listener.attribute_updated(attr, values[attr], None)
    except Exception as exc:  # noqa: BLE001 — a sleeping device is not a failure
        logger.info("Zigbee: initial read of 0x%04X failed, waiting for a report instead: %s", attr, exc)
