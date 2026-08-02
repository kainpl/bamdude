"""Binding a plug's clusters and routing their reports into the driver cache.

``bind`` tells the device where to send reports; ``configure_reporting`` says
which attributes and how often. Both are set up here, and both are treated as a
bonus rather than the mechanism: reports are welcome when they arrive, and
``poller.py`` keeps the cache honest whether they do or not.

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
from zigpy.zdo import types as zdo_types

from backend.app.core.tasks import spawn_background_task
from backend.app.services.zigbee.devices import ELECTRICAL_MEASUREMENT, METERING, ON_OFF
from backend.app.services.zigbee.errors import describe_exception
from backend.app.services.zigbee.metering import (
    ENERGY_SCALING_ATTRS,
    POWER_SCALING_ATTRS,
    scale,
)

logger = logging.getLogger(__name__)

# zigpy's current event names. "attribute_report" carries a device's own report;
# "attribute_updated" fires when the value was written some other way (a read, a
# quirk transforming the report). Report handling deliberately suppresses the
# LEGACY listener_event of the same name, so subscribing only through
# add_listener leaves a subscription that looks alive and never fires.
ATTRIBUTE_REPORT_EVENT = "attribute_report"
ATTRIBUTE_UPDATED_EVENT = "attribute_updated"

# Attribute ids. ON_OFF and SUMMATION collide at 0x0000 by design of the ZCL —
# see the module docstring for why that forces per-cluster listeners.
ATTR_ON_OFF = 0x0000
ATTR_SUMMATION = 0x0000
ATTR_ACTIVE_POWER = 0x050B

# Reporting bounds. The minimum keeps a chattering device from flooding the
# mesh; the maximum is a heartbeat, so a plug that never changes still proves it
# is alive rather than looking unreachable forever.
_MIN_INTERVAL = 5
_MAX_INTERVAL = 900
_REPORTABLE_CHANGE = 1

# The three clusters we bind, subscribe and poll, with the scaling pair each one
# needs and the attribute that carries its reading. One table so bind and poll
# can never drift apart — a cluster subscribed but not polled is exactly the
# silent half-configuration this module keeps rediscovering.
#
# **On/Off comes first, and the order is load-bearing.** The S60ZBTPF quirk gates
# power on the socket's cached on_off value, so reading power before on_off in the
# same cycle would judge a freshly-switched-on socket by its previous state and
# discard the first real measurement.
_POLLED_CLUSTERS = (
    (ON_OFF, (), ATTR_ON_OFF),
    (METERING, ENERGY_SCALING_ATTRS, ATTR_SUMMATION),
    (ELECTRICAL_MEASUREMENT, POWER_SCALING_ATTRS, ATTR_ACTIVE_POWER),
)


def _subscribe_cluster(cluster, listener, who: str) -> None:
    """Subscribe a listener on every channel zigpy delivers through.

    ``on_event`` is the one that carries device reports in zigpy 2.x;
    ``add_listener`` still fires for attributes zigpy does not recognise and for
    values written outside report handling. Recording is idempotent, so the
    overlap costs nothing — and a cluster with no ``on_event`` says so loudly,
    because a subscription that looks alive and never fires is what cost a
    hardware session to find.
    """
    on_event = getattr(cluster, "on_event", None)
    if on_event is None:
        logger.warning(
            "Zigbee %s: cluster 0x%04X has no on_event — device reports will NOT arrive, only polled reads",
            who,
            getattr(cluster, "cluster_id", 0),
        )
    else:
        for event_name in (ATTRIBUTE_REPORT_EVENT, ATTRIBUTE_UPDATED_EVENT):
            on_event(event_name, listener)
    cluster.add_listener(listener)


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

    def __call__(self, event) -> None:
        """zigpy's current event API, which is what actually carries reports.

        ``Report_Attributes`` handling suppresses the legacy ``listener_event``
        for known attributes, so a plug subscribed only through ``add_listener``
        never hears from the device: every "reported state" line came from
        ``_read_into_cache`` after a poll, and ``configure_reporting`` was
        decorative. Same routing either way, so a report and a read cannot
        disagree about what a counter means.
        """
        try:
            attribute_id = getattr(event, "attribute_id", None)
            if attribute_id is None:
                return
            self.attribute_updated(attribute_id, getattr(event, "value", None))
        except Exception as exc:  # noqa: BLE001 — an event callback must not raise into zigpy
            logger.warning("Zigbee plug %s: event ignored: %s", self._plug_id, exc)

    def attribute_updated(self, attrid, value, timestamp=None) -> None:
        """zigpy calls this synchronously — plain ``def``, never ``async def``.

        An ``async def`` would return a coroutine nobody runs and every report
        would vanish without a trace. zigpy also swallows exceptions from
        listeners at DEBUG level, so the guard below exists to make failures
        visible rather than to protect zigpy.
        """
        try:
            if self._cluster_id == ON_OFF and attrid == ATTR_ON_OFF:
                state = "ON" if value else "OFF"
                # Logged at INFO, unlike the energy reports below: whether
                # on/off reports arrive at all is the question that took a
                # hardware session to answer, and an empty log looked the same
                # as a working subscription.
                logger.info("Zigbee plug %s reported state %s", self._plug_id, state)
                self._service.update(self._plug_id, state=state)
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


async def _scaling_pair(cluster, attrs: tuple[str, ...]):
    """Read a cluster's multiplier/divisor once, trying pairs in order.

    ``attrs`` is (multiplier, divisor) pairs flattened, most-preferred first —
    all of them fetched in a single read, then first-hit wins. One read rather
    than one per pair: they are device constants and the fallback exists only
    because some devices implement the second pair instead of the first.

    Failure returns ``(None, None)``, which downstream turns into "no reading"
    rather than a guess — a device that will not say what its counter means has
    not told us anything.
    """
    try:
        result = await cluster.read_attributes(list(attrs))
        values = result[0] if isinstance(result, (list, tuple)) else result
    except Exception as exc:  # noqa: BLE001 — an unreadable scale is not fatal
        logger.warning("Zigbee: could not read scaling attributes %s: %s", attrs, exc)
        return None, None

    values = values or {}
    for i in range(0, len(attrs), 2):
        multiplier, divisor = values.get(attrs[i]), values.get(attrs[i + 1])
        if divisor is not None:
            return multiplier, divisor
    return None, None


async def bind_plug(service, plug, device) -> dict[int, bool]:
    """Bind and subscribe every cluster this plug actually has.

    Best-effort per cluster: a plug whose Metering refuses to bind should still
    switch. Returns which clusters were wired so the caller can log a plug that
    will never report energy, instead of leaving it to be discovered as a
    permanent absence of readings.
    """
    wired: dict[int, bool] = {}

    for cluster_id, scaling_attrs, attr in _POLLED_CLUSTERS:
        cluster = service._cluster(device, cluster_id)
        if cluster is None:
            wired[cluster_id] = False
            continue

        multiplier = divisor = None
        if scaling_attrs:
            multiplier, divisor = await _scaling_pair(cluster, scaling_attrs)

        # Re-binding reuses the listener that is already subscribed rather than
        # adding a second one. Multiplier and divisor are device constants, so
        # the existing object is equivalent — and the reads below have to route
        # through the same object the events do, or a report and a poll would
        # be handled by two listeners with two identities.
        already = (str(getattr(device, "ieee", plug.id)).strip().lower(), cluster_id) in _attached_clusters
        listener = service._listeners.get((plug.id, cluster_id)) if already else None
        if listener is None:
            listener = ClusterReportListener(
                service=service,
                plug_id=plug.id,
                cluster_id=cluster_id,
                multiplier=multiplier,
                divisor=divisor,
            )
            already = False
        service._listeners[(plug.id, cluster_id)] = listener
        try:
            if not already:
                _subscribe_cluster(cluster, listener, f"plug {plug.id}")
                _attached_clusters.add((str(getattr(device, "ieee", plug.id)).strip().lower(), cluster_id))
            bind_result = await cluster.bind()
            _warn_if_bind_refused(plug.id, cluster_id, bind_result)
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
        await _read_into_cache(cluster, listener, attr)

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

    The result is a **dict** of ``{ZCLAttributeDef: Status}``. The first version
    of this function iterated it as a list of records and read ``.status`` off
    each element — iterating a dict yields keys, which have no such attribute,
    so it never fired once. It was written to catch a silent refusal and was
    itself silent, which is worse than not having it: an empty log read as proof
    the device had accepted.

    An unrecognised shape is therefore logged rather than swallowed. If this
    check stops matching a future zigpy, that should be visible.
    """
    if not isinstance(result, dict):
        logger.debug(
            "Zigbee plug %s: unexpected configure_reporting result for cluster 0x%04X: %r",
            plug_id,
            cluster_id,
            result,
        )
        return

    for attr, status in result.items():
        if status == foundation.Status.SUCCESS:
            continue
        logger.warning(
            "Zigbee plug %s: device refused reporting for %s on cluster 0x%04X (status=%s). "
            "Its state will only update when something else reads it.",
            plug_id,
            getattr(attr, "name", attr),
            cluster_id,
            status,
        )


def _warn_if_bind_refused(plug_id: int, cluster_id: int, result) -> None:
    """``bind()`` does not raise on a ZDO refusal either — it returns the status.

    Same shape of bug as ``configure_reporting``: the call returns cleanly, the
    log says reporting is set up, and no report ever arrives. Without the device
    accepting the binding it has nowhere to send them, so this is the first thing
    to look at when the cache only ever moves on a restart.

    The response is a list whose first element is the ZDO status. An unfamiliar
    shape is logged rather than assumed good.
    """
    status = result[0] if isinstance(result, (list, tuple)) and result else result
    if status == zdo_types.Status.SUCCESS:
        return
    logger.warning(
        "Zigbee plug %s: device refused the binding for cluster 0x%04X (status=%r). "
        "It will not send reports; readings will only update when we read it.",
        plug_id,
        cluster_id,
        status,
    )


async def _read_into_cache(cluster, listener: ClusterReportListener, attr: int) -> bool:
    """Refresh one attribute from the device, then cache the value the quirk left.

    The value is taken from the cluster's own attribute cache, **not** from what
    ``read_attributes`` returns, and both halves of that matter:

    * zigpy suppresses the ``attribute_updated`` event for the attribute being
      read (``_legacy_apply_quirk_attribute_update`` wraps the update in
      ``_suppress_attribute_update_event``), so a listener never hears a read and
      waiting for the event would cache nothing at all;
    * the returned dict holds the **raw** value, from before quirks ran. Our
      plug's firmware keeps reporting the last measured power after its socket is
      switched off, and the quirk for it swallows exactly that value — reading
      the response dict walks straight past the fix and caches 33 W for a socket
      with nothing running. That was the bug.

    ZHA reads the same way round: ``safe_read`` to refresh the cluster, then
    ``cluster.get(name)`` to use the value.

    Still routed through the listener rather than written to the cache directly,
    so a read and a report get identical scaling and mapping — two paths into one
    cache is how they come to disagree.
    """
    try:
        await cluster.read_attributes([attr], allow_cache=False, only_cache=False)
    except Exception as exc:  # noqa: BLE001 — a sleeping or absent device is not a failure
        logger.info("Zigbee: read of 0x%04X failed, waiting for a report instead: %s", attr, describe_exception(exc))
        return False

    value = cluster.get(attr)
    if value is None:
        return False
    listener.attribute_updated(attr, value, None)
    return True


async def refresh_plug(service, plug, device) -> bool:
    """Read every subscribed attribute once, now.

    Called on the poller's timer and, off-cycle, when a caller asks about a plug
    whose cache has aged out. Reads have proved reliable on the same mesh where
    unsolicited reports never arrived, which is why this — not reporting — is
    what the freshness of a reading actually rests on.
    """
    ok = False
    for cluster_id, scaling_attrs, attr in _POLLED_CLUSTERS:
        cluster = service._cluster(device, cluster_id)
        if cluster is None:
            continue

        # Reuse the bind-time listener: it already holds this cluster's
        # multiplier and divisor, which never change. Building a fresh one here
        # would re-read both on every refresh — two extra round-trips per
        # cluster, on the shared radio, for a constant.
        listener = service._listeners.get((plug.id, cluster_id))
        if listener is None:
            multiplier = divisor = None
            if scaling_attrs:
                multiplier, divisor = await _scaling_pair(cluster, scaling_attrs)
            listener = ClusterReportListener(
                service=service,
                plug_id=plug.id,
                cluster_id=cluster_id,
                multiplier=multiplier,
                divisor=divisor,
            )
            service._listeners[(plug.id, cluster_id)] = listener
            cluster.add_listener(listener)

        if await _read_into_cache(cluster, listener, attr):
            ok = True
    return ok


# --- sensors -----------------------------------------------------------------
#
# Sensors reuse this module rather than getting a copy of it. Bind, configure
# and listen are the part of the subsystem where a mistake is invisible: not
# "the plug will not switch" but "the number looks right and is not". Two
# copies would mean fixing the next quirk bug twice and forgetting the second.
#
# The plug path above is untouched. Its multiplier/divisor scaling is a metering
# concern and has no place in a registry of ambient quantities.


class SensorReportListener:
    """Routes one sensor cluster's attribute reports into the sensor store.

    One listener per cluster, and the attribute id is what distinguishes the
    quantities on it: Power Configuration 0x0001 carries both the battery
    percentage (0x0021) and the battery voltage (0x0020). Dispatching on the
    cluster alone would file a voltage as a percentage — the same shape of bug
    as the shared 0x0000 on plugs, documented at the top of this module.

    It also holds the device, because **a report is the proof that a sleeper is
    awake** — the one moment changed reporting parameters can be pushed to it.
    """

    def __init__(self, store, ieee: str, cluster_id: int, device=None, keys: tuple[str, ...] = (), cluster=None):
        self._store = store
        self._ieee = ieee
        self._cluster_id = cluster_id
        self._device = device
        self._keys = keys
        self._cluster = cluster
        self._by_attribute_id: dict[int, str] = {}
        self._reported: set[str] = set()

    def __call__(self, event) -> None:
        """zigpy's current event API hands the subscriber a dataclass.

        This is the path that actually carries device reports. ``Report_Attributes``
        handling wraps its cache write in ``_suppress_attribute_update_event``,
        so the legacy ``listener_event("attribute_updated")`` never fires for a
        reported attribute — subscribing only that way makes a dead
        subscription look alive.
        """
        try:
            self._record(getattr(event, "attribute_id", None), getattr(event, "value", None))
        except Exception as exc:  # noqa: BLE001 — an event callback must not raise into zigpy
            logger.warning("Zigbee sensor %s: event ignored: %s", self._ieee, exc)

    def bind_attribute(self, attribute_id: int, key: str) -> None:
        """Learn which quantity an attribute id carries.

        Resolved once at bind time from the cluster's own definitions rather
        than hardcoded, so a quirk that redefines an attribute is followed
        instead of contradicted.
        """
        self._by_attribute_id[attribute_id] = key

    def attribute_updated(self, attrid, value, timestamp=None) -> None:
        """The legacy path, kept as a second belt.

        zigpy still calls it for attributes it does not know and for values
        written outside report handling. Plain ``def``, never ``async def``, and
        it must not raise: zigpy logs listener exceptions at DEBUG, so a failure
        here would vanish rather than break.
        """
        try:
            self._record(attrid, value)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            logger.warning("Zigbee sensor %s: report for 0x%04X ignored: %s", self._ieee, attrid, exc)

    def _record(self, attrid, value) -> None:
        """One place where a value becomes a reading, whichever path delivered it."""
        key = self._by_attribute_id.get(attrid)
        if key is None:
            return  # devices report far more than they were asked for

        # From the cluster cache rather than the event payload: the cache is
        # what a quirk has had its say over, and this subsystem has one rule
        # about where a value comes from. The payload is the fallback for a
        # cluster we were handed without one.
        cached = None
        if self._cluster is not None:
            from backend.app.services.zigbee.measurements import BY_KEY

            measurement = BY_KEY.get(key)
            if measurement is not None:
                cached = self._cluster.get(measurement.attribute)
        value = cached if cached is not None else value

        # The first report of each quantity is logged at INFO, the rest at
        # DEBUG — the same reasoning as the plug listener's state line:
        # whether reports arrive AT ALL is the question that costs a hardware
        # session to answer, and an empty log looks identical to a working
        # subscription.
        if key not in self._reported:
            self._reported.add(key)
            logger.info("Zigbee sensor %s: first %s report received (raw=%r)", self._ieee, key, value)
        else:
            logger.debug("Zigbee sensor %s reported %s=%r", self._ieee, key, value)
        self._store.record(self._ieee, key, value)
        if self._device is not None:
            # The device is demonstrably awake right now. Hanging the re-apply
            # on the watchdog alone would defer an operator's edit by a whole
            # silence window — and the watchdog only runs once the device has
            # gone quiet, which is exactly when it cannot hear us.
            spawn_background_task(
                reapply_if_settings_changed(self._device, self._ieee, self._keys),
                name=f"zigbee-reapply-{self._ieee}",
            )


def _warn_if_sensor_reporting_refused(ieee: str, cluster_id: int, result) -> None:
    """The plug version of this takes a plug id; a sensor has no row and no id.

    Worth its own function rather than passing 0: "Zigbee plug 0 refused
    reporting" in the log would name a device that does not exist, in exactly
    the place the log is being read to find out what happened.
    """
    if not isinstance(result, dict):
        logger.debug(
            "Zigbee sensor %s: unexpected configure_reporting result for cluster 0x%04X: %r", ieee, cluster_id, result
        )
        return
    for attr, status in result.items():
        if status == foundation.Status.SUCCESS:
            continue
        logger.warning(
            "Zigbee sensor %s: device refused reporting for %s on cluster 0x%04X (status=%s). "
            "Its readings will only update when something else reads it.",
            ieee,
            getattr(attr, "name", attr),
            cluster_id,
            status,
        )


def _attribute_ids(cluster) -> dict[str, int]:
    """Map attribute name to id from the cluster's own definitions."""
    ids: dict[str, int] = {}
    for definition in getattr(cluster, "AttributeDefs", ()) or ():
        name = getattr(definition, "name", None)
        attribute_id = getattr(definition, "id", None)
        if name is not None and attribute_id is not None:
            ids[name] = attribute_id
    return ids


def _sensor_clusters(device) -> dict[int, object]:
    """Every cluster on the device that carries a registered quantity."""
    from backend.app.services.zigbee.measurements import measurements_on

    found: dict[int, object] = {}
    for endpoint in (getattr(device, "endpoints", None) or {}).values():
        for cluster_id, cluster in (getattr(endpoint, "in_clusters", None) or {}).items():
            if measurements_on(cluster_id):
                found.setdefault(cluster_id, cluster)
    return found


# Which (ieee, cluster id) pairs already carry a listener in this process.
# bind_sensor attaches before it touches the radio, so every re-apply of changed
# settings would otherwise add another listener to the same cluster — one more
# duplicate report each time, growing for the life of the process. Seen in the
# field as every first-report line printed twice.
_attached_clusters: set[tuple[str, int]] = set()


def forget_sensor_listeners(ieee: str) -> None:
    """Drop the attachment record for an unpaired device.

    Idempotence must not outlive the device: pairing it again hands back new
    cluster objects, and a stale record would leave those unheard.
    """
    marker = str(ieee).strip().lower()
    for key in [k for k in _attached_clusters if k[0] == marker]:
        _attached_clusters.discard(key)


def attach_sensor_listeners(device, ieee: str, keys: tuple[str, ...] = (), store=None) -> int:
    """Route this device's reports into the store. **Local — no radio.**

    Split out of :func:`bind_sensor` because the two halves have different
    requirements, and conflating them cost a working sensor. Binding and
    configuring need the device awake; attaching a listener is a local object
    operation that works whether it is asleep, unreachable or flat. Attaching
    only at pairing meant that after a restart the device's reports reached
    zigpy and were dropped — it looked perfectly paired and stayed blank for
    ever. Found on hardware, not by the suite.
    """
    from backend.app.services.zigbee.measurements import measurements_on
    from backend.app.services.zigbee.sensors import sensor_store

    marker = str(ieee).strip().lower()
    attached = 0
    for cluster_id, cluster in _sensor_clusters(device).items():
        if (marker, cluster_id) in _attached_clusters:
            attached += 1
            continue
        wanted = measurements_on(cluster_id)
        listener = SensorReportListener(
            store=sensor_store if store is None else store,
            ieee=ieee,
            cluster_id=cluster_id,
            device=device,
            keys=keys or tuple(m.key for m in wanted),
            cluster=cluster,
        )
        attribute_ids = _attribute_ids(cluster)
        for measurement in wanted:
            attribute_id = attribute_ids.get(measurement.attribute)
            if attribute_id is not None:
                listener.bind_attribute(attribute_id, measurement.key)
        try:
            # Both channels. ``on_event`` is the one that carries device reports
            # in zigpy 2.x; ``add_listener`` still fires for attributes zigpy
            # does not recognise and for writes outside report handling.
            # Recording is idempotent, so the overlap costs nothing.
            _subscribe_cluster(cluster, listener, f"sensor {ieee}")
            _attached_clusters.add((marker, cluster_id))
            attached += 1
        except Exception as exc:  # noqa: BLE001 — one cluster must not cost the others
            logger.warning("Zigbee sensor %s: could not listen on 0x%04X: %s", ieee, cluster_id, exc)
    return attached


def attach_all_sensors(app) -> int:
    """Attach listeners for every paired sensor. Called once at startup.

    The counterpart of :func:`subscribe_all` for plugs, needed for the same
    reason — except this one asks nothing of the devices, so a sleeping sensor
    is served as well as a waking one.
    """
    from backend.app.services.zigbee.devices import DeviceKind, describe_device

    attached = 0
    for device in list(getattr(app, "devices", {}).values()):
        info = describe_device(device)
        if info.kind is not DeviceKind.SENSOR:
            continue
        if attach_sensor_listeners(device, info.ieee, info.measurements):
            attached += 1
    return attached


async def bind_sensor(device, ieee: str, parameters: dict[str, dict]) -> dict[str, str]:
    """Attach listeners AND ask the device to report. Needs it awake.

    Called at pairing, while the device is provably awake, and again when a
    settings change is owed and the device has just spoken. The listener half is
    delegated to :func:`attach_sensor_listeners`, which the startup path calls
    on its own — that much needs no radio.

    ``parameters`` is the operator's desired settings keyed by measurement; any
    missing field falls back to the registry default. Returns per-measurement
    ``"ok"`` / ``"refused"`` so the caller can surface what the device actually
    accepted rather than leaving it to be inferred from silence — which is how
    the plugs' reporting was believed to work for a whole phase.
    """
    from backend.app.services.zigbee.measurements import measurements_on, to_raw_change

    applied: dict[str, str] = {}
    clusters = _sensor_clusters(device)
    attach_sensor_listeners(device, ieee)

    for cluster_id, cluster in clusters.items():
        for measurement in measurements_on(cluster_id):
            settings = parameters.get(measurement.key, {})
            min_interval = int(settings.get("min_interval", measurement.default_min_interval))
            max_interval = int(settings.get("max_interval", measurement.default_max_interval))
            change = float(settings.get("reportable_change", measurement.default_reportable_change))
            try:
                await cluster.bind()
                result = await cluster.configure_reporting(
                    measurement.attribute, min_interval, max_interval, to_raw_change(measurement, change)
                )
                _warn_if_sensor_reporting_refused(ieee, cluster_id, result)
                applied[measurement.key] = "ok"
            except Exception as exc:  # noqa: BLE001 — one quantity failing must not lose the others
                logger.warning(
                    "Zigbee sensor %s: could not configure %s on 0x%04X: %s",
                    ieee,
                    measurement.key,
                    cluster_id,
                    describe_exception(exc),
                )
                applied[measurement.key] = "refused"

    return applied


# One re-apply in flight per device. A sensor reports several quantities within
# the same second, and without this each report would start its own bind.
_reapplying: set[str] = set()


async def reapply_if_settings_changed(device, ieee: str, keys: tuple[str, ...]) -> None:
    """Push changed reporting parameters to a device that is awake right now.

    Reporting parameters live IN the device: editing a setting does nothing
    until ``configure_reporting`` is re-issued. The call is idempotent, so
    re-issuing when in doubt is cheap — which is what makes "applied is unknown
    after a restart" a non-problem rather than a gap.
    """
    from backend.app.core.database import async_session
    from backend.app.services.zigbee.coordinator import zigbee_coordinator
    from backend.app.services.zigbee.sensor_settings import load_reporting_parameters

    marker = str(ieee).strip().lower()
    if marker in _reapplying:
        return
    _reapplying.add(marker)
    try:
        async with async_session() as db:
            parameters = await load_reporting_parameters(db)
        desired = {key: parameters.get(key, {}) for key in keys}
        if not desired or zigbee_coordinator.desired_reporting(ieee) == desired:
            return
        applied = await bind_sensor(device, ieee, parameters)
        zigbee_coordinator.record_reporting(ieee, desired, applied)
        logger.info("Zigbee sensor %s: reporting re-applied from settings: %s", ieee, applied)
    except Exception as exc:  # noqa: BLE001 — a failed re-apply must not break the report path
        logger.debug("Zigbee sensor %s: could not re-apply reporting: %s", ieee, describe_exception(exc))
    finally:
        _reapplying.discard(marker)
