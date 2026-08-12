"""The Zigbee smart-plug driver.

Implements the same four methods every other plug type does — ``get_status``,
``turn_on``, ``turn_off``, ``get_energy``, plus ``toggle`` — so
``SmartPlugManager`` reaches it with one branch and everything already built on
that interface follows: the control endpoint, the scheduler, auto-off after a
print, and Obico's "pause and cut power".

State and readings come from a cache fed by two sources: attribute reports when
the device sends them, and a background poll every 30–45 s.

The poll is not belt-and-braces. Reporting on the ElectricalMeasurement cluster
is unreliable across real plugs, and the reference implementation says so
outright: ZHA polls that cluster on a 30–45 s timer for **every** device except
four models hardcoded as trustworthy (``ElectricalMeasurementPoller`` vs
``ElectricalMeasurementReportingDevice`` in ``zha/application/platforms/sensor``).
The plug this was built against is not one of the four. An earlier version of
this driver refused to poll on principle and reported minutes-old wattage as
current — the principle was wrong.

Two rules carried over from phase 0, both about the same asymmetry — turning a
printer on unexpectedly is an annoyance, turning it off mid-print is not:

* ``toggle`` refuses when the state is unknown rather than guessing;
* a comms failure never changes the recorded state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from zigpy.zcl import foundation

from backend.app.services.zigbee.coordinator import CoordinatorState, zigbee_coordinator
from backend.app.services.zigbee.devices import ELECTRICAL_MEASUREMENT, METERING, ON_OFF
from backend.app.services.zigbee.errors import describe_exception

logger = logging.getLogger(__name__)

# On/Off cluster command ids (ZCL). Named rather than inlined — a bare 0 and 1
# in a switch call is exactly what gets transposed in a later edit.
_CMD_OFF = 0x00
_CMD_ON = 0x01

# How long a cached reading may be trusted. Past it the driver reads the device
# instead of answering from cache, and reports unreachable if that read fails.
#
# Found the hard way: without any window the driver answered "power: 32" for a
# plug that had been switched OFF, from a value read once at bind time.
# ``reachable`` only asked whether the cache held *anything*, so a twenty-minute
# old number was presented as current. A stale reading is worse than none — it
# reads as a measurement.
#
# 120 s, i.e. roughly three of the poller's 30–45 s cycles.
#
# Briefly tried at 90 to make an unplugged device disappear sooner. Reverted:
# 90 is *exactly* two worst-case cycles, so two consecutive polls landing at the
# top of the jitter range put a perfectly healthy plug on the edge of being
# declared unreachable. Trading a false "offline" on a working plug for slightly
# faster detection is the wrong way round — the operator acts on offline.
#
# The latency this was meant to buy is not needed here anyway: this window is
# the **backstop**, not the mechanism. A radio that goes down is reported at once
# through the coordinator's own status (see ``_device_for``), which covers the
# case people actually notice. What is left for this constant is one device
# going quiet while the mesh is otherwise fine — rare, and worth being sure
# about rather than quick about.
_STALE_AFTER_SECONDS = 120

# How long each kind of caller may wait for a read, and why they differ.
#
# The poller exists to absorb the slow case: three clusters, each read costing
# up to zigpy's own ~8 s before it gives up on a silent device. Nobody is
# watching it, so it gets room to finish.
#
# An HTTP handler has no such licence. Measured on hardware with a plug pulled
# out of the wall: individual status requests sat in these reads for 28–74 s.
# The API process itself stayed healthy throughout — ``/health`` answered in
# 0.21 s the whole time — but a browser allows only ~6 connections per origin,
# so six such requests consume every socket the tab has and it can no longer
# issue anything, including its own reload. The page looked frozen and the
# server was fine. A request must therefore be bounded well under the point
# where sockets start piling up; three seconds is far longer than a healthy
# device has ever taken to answer, and short enough that the pile cannot form.
_REQUEST_REFRESH_BUDGET_SECONDS = 3.0
_POLLER_REFRESH_BUDGET_SECONDS = 60.0


def _command_succeeded(result) -> bool:
    """Whether the device's Default Response says the command was carried out.

    A refused command does not raise — it answers, and the answer is the only
    place the refusal appears. ZHA reads the same field
    (``zha/application/platforms/switch.py``: ``if result[1] is not
    Status.SUCCESS: raise``).

    An unrecognised shape counts as success. Some commands legitimately return
    nothing, and treating "no response frame to inspect" as a failure would
    report working plugs as broken — the opposite of the caution wanted here,
    since the caller's fallback is to leave the plug's state unknown.
    """
    try:
        status = result[1]
    except (TypeError, IndexError, KeyError):
        return True
    return status == foundation.Status.SUCCESS


@dataclass
class ZigbeePlugData:
    """What reporting has told us about one plug so far."""

    state: str | None = None
    power: float | None = None
    energy_total: float | None = None
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ZigbeeSmartPlugService:
    # Every answer here comes from the cache, which reports and the poller fill.
    # The other plug types read live over HTTP or hold state the device pushed,
    # so a caller who needs the newest possible reading — the two ends of a
    # per-print energy measurement — can only ask this one to go and look.
    reads_from_a_cache = True

    def __init__(self):
        self._cache: dict[int, ZigbeePlugData] = {}
        # Listeners built at bind time, kept so an on-demand read can reuse one
        # instead of rebuilding it. They carry the cluster's multiplier/divisor,
        # which are device constants — re-reading them per refresh would put two
        # mesh round-trips behind a number that never changes.
        self._listeners: dict[tuple[int, int], Any] = {}
        # One read per plug at a time, shared by everyone who asks while it runs.
        # See ``refresh`` for why this is not merely an optimisation.
        self._refreshing: dict[int, asyncio.Task] = {}
        # Per-plug staleness, loaded from zigbee_devices when a plug is wired.
        # Absent means the module default — which is every plug that nobody has
        # set a value for, and was every plug before this was configurable.
        self._stale_after: dict[int, int] = {}

    # ---- cache, written by the reporting listener ---------------------------

    def get_plug_data(self, plug_id: int) -> ZigbeePlugData | None:
        return self._cache.get(plug_id)

    def update(self, plug_id: int, **fields) -> None:
        """Merge a report into the cache.

        Merges rather than replaces: reports arrive per attribute, so a power
        update must not blank the energy total that came in a second earlier.
        """
        data = self._cache.setdefault(plug_id, ZigbeePlugData())
        for key, value in fields.items():
            if value is not None:
                setattr(data, key, value)
        data.last_seen = datetime.now(timezone.utc)

    # ---- device resolution --------------------------------------------------

    def _device_for(self, plug: Any):
        """The zigpy device for a plug, or None.

        None covers every unreachable case there is — no IEEE configured, radio
        down, device not on the mesh. They are collapsed on purpose: every
        caller does the same thing with the answer, and separating them would
        only invite a branch that reports one of them as an error when the
        operator-visible result is identical.
        """
        ieee = getattr(plug, "zigbee_ieee", None)
        app = zigbee_coordinator.app
        if not ieee or app is None:
            return None
        # A lost radio does NOT clear ``app`` — ``connection_lost`` only moves
        # the status to ERROR, so the application object and its device table
        # outlive the transport. Reading "there is an app" as "the radio works"
        # therefore left every plug looking healthy for the whole staleness
        # window after the dongle went away: the cache was recent, so nothing
        # asked the radio anything, and the card kept saying the plug was fine.
        #
        # Checking the status makes the answer immediate and, being here rather
        # than in each caller, it cascades to every plug at once — which is the
        # honest reading, since one dead coordinator means none of them are
        # reachable. Nothing is sent to the devices: this returns before any
        # I/O, so no command goes out to hardware that cannot receive it.
        if zigbee_coordinator.status.state is not CoordinatorState.UP:
            return None
        wanted = str(ieee).strip().lower()
        return next((d for k, d in app.devices.items() if str(k).lower() == wanted), None)

    def _cluster(self, device, cluster_id: int):
        """A cluster from any endpoint, not endpoint 1 by assumption.

        Phase 2 established that a plug may put On/Off elsewhere; the same holds
        for Metering. Endpoint numbering is not something the spec guarantees.
        """
        if device is None:
            return None
        for endpoint in (getattr(device, "endpoints", None) or {}).values():
            cluster = (getattr(endpoint, "in_clusters", None) or {}).get(cluster_id)
            if cluster is not None:
                return cluster
        return None

    # ---- the plug driver interface ------------------------------------------

    async def _switch(self, plug: Any, command_id: int, what: str) -> bool:
        cluster = self._cluster(self._device_for(plug), ON_OFF)
        if cluster is None:
            logger.info("Zigbee plug %s: device not reachable, ignoring %s", plug.id, what)
            return False
        try:
            result = await cluster.command(command_id)
        except Exception as exc:  # noqa: BLE001 — a switch failure is reported, never raised
            # The cache is deliberately untouched. Writing the state we hoped
            # for is how automation comes to believe a printer is powered when
            # it is not.
            logger.warning("Zigbee plug %s: %s failed: %s", plug.id, what, describe_exception(exc))
            return False

        if not _command_succeeded(result):
            logger.warning("Zigbee plug %s: device refused %s (%r)", plug.id, what, result)
            return False

        # The device acknowledged the command, so record the new state now. This
        # is not the hoped-for state phase 0 ruled out — it is the device's own
        # Default Response saying it did what was asked. ZHA does exactly this
        # (``Switch.async_turn_on`` checks the status, then writes the attribute
        # through) rather than reading back, and the reason is visible here: a
        # read-back costs a round-trip and still cannot beat the ack.
        state = "ON" if command_id == _CMD_ON else "OFF"

        # Power now describes a load that no longer exists. The plug's own power
        # register updates on its schedule, not ours, so anything held at this
        # moment belongs to the previous state — which is how "33 W" got reported
        # for a socket that had just been switched off. Dropped first so that a
        # quirk's authoritative zero, written below, wins over this.
        self._invalidate_power(plug.id)

        self.update(plug.id, state=state)
        self._tell_the_cluster(cluster, plug, state)
        return True

    def _tell_the_cluster(self, cluster, plug: Any, state: str) -> None:
        """Write the new state into the cluster's own attribute cache too.

        Not redundant with the line above it. Quirks hang off
        ``Cluster._update_attribute``, and the one for this plug reacts to the
        socket going off by zeroing power and current — so a switch that never
        reaches the cluster leaves the quirk believing the socket is still in its
        old state, and the next poll is judged by that. ZHA writes the attribute
        through for the same reason (``Switch.async_turn_on``).

        The driver's own cache is still updated directly rather than left to the
        event this fires: a plug whose listener was never attached must still
        report the state it was just switched to.
        """
        from backend.app.services.zigbee.reporting import ATTR_ON_OFF

        try:
            cluster.update_attribute(ATTR_ON_OFF, 1 if state == "ON" else 0)
        except Exception as exc:  # noqa: BLE001 — the switch itself already succeeded
            logger.debug("Zigbee plug %s: could not update the cluster cache: %s", plug.id, exc)

    async def turn_on(self, plug: Any) -> bool:
        return await self._switch(plug, _CMD_ON, "turn on")

    async def turn_off(self, plug: Any) -> bool:
        return await self._switch(plug, _CMD_OFF, "turn off")

    async def toggle(self, plug: Any) -> bool:
        """Flip a known state; refuse an unknown one.

        The other drivers toggle blind because their transport answers
        synchronously. Here the state comes from a report cache that is empty
        until the device first speaks — so a blind toggle is a coin flip on
        cutting power to a running print.
        """
        data = self.get_plug_data(plug.id)
        if data is None or data.state is None:
            logger.info("Zigbee plug %s: state unknown, refusing to toggle", plug.id)
            return False
        return await (self.turn_off(plug) if data.state == "ON" else self.turn_on(plug))

    def _invalidate_power(self, plug_id: int) -> None:
        """Forget the power reading, keeping everything else.

        ``update`` cannot do this: it ignores None so that a report about one
        attribute never blanks another. Dropping a value is a different act from
        merging one, and it needs its own door.
        """
        data = self._cache.setdefault(plug_id, ZigbeePlugData())
        data.power = None

    def set_stale_after(self, plug_id: int, seconds: int | None) -> None:
        """How long this plug's reading may go unrefreshed before it is doubted.

        None clears the override rather than storing a zero: "not set" and "set
        to nothing" are different answers, and only one of them means the
        default applies.
        """
        if seconds:
            self._stale_after[plug_id] = int(seconds)
        else:
            self._stale_after.pop(plug_id, None)

    def _is_stale(self, data, plug_id: int) -> bool:
        last_seen = getattr(data, "last_seen", None)
        if last_seen is None:
            return True
        threshold = self._stale_after.get(plug_id, _STALE_AFTER_SECONDS)
        return (datetime.now(timezone.utc) - last_seen).total_seconds() > threshold

    def _forget_refresh(self, task: asyncio.Task) -> None:
        """Drop a finished read, and take its exception with it.

        The second half is not tidiness. Because callers time out independently
        of the read, the last one can walk away *before* it finishes — and a task
        that then raises with nobody left to await it surfaces at garbage
        collection as a bare "Task exception was never retrieved", detached from
        the plug it belonged to. Retrieving it here means it is always attributed.
        """
        for plug_id, running in list(self._refreshing.items()):
            if running is task:
                del self._refreshing[plug_id]
                break
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Zigbee: a plug read failed: %s", describe_exception(task.exception()))

    async def cancel_refreshes(self) -> None:
        """Stop every read in flight. For when the radio is going away.

        The tasks hold zigpy cluster objects belonging to the application being
        torn down, so leaving them running means reads against a dead radio —
        the same orphaned-cluster shape the listener cache is cleared to avoid.
        """
        tasks = list(self._refreshing.values())
        self._refreshing.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def refresh(self, plug: Any, timeout: float = _POLLER_REFRESH_BUDGET_SECONDS) -> bool:
        """Read every subscribed attribute from the device, now.

        Both the background poller and the staleness path below go through here,
        so there is one way a reading enters the cache from a read. What differs
        between them is how long each may wait, and that difference is the whole
        point of this method's shape.

        **A caller's timeout never cancels the read.** The in-flight task is
        shared and shielded: a caller that runs out of patience walks away, the
        read carries on, and whoever asks next gets the answer it lands in the
        cache. Cancelling it instead would mean the impatient caller had made
        the situation worse for everybody — and the poller, whose entire job is
        to absorb the slow case, would keep losing its work to a page refresh.

        **One read per plug, however many ask.** Measured on hardware: with a
        plug pulled out of the wall, four overlapping status requests each
        started their own three reads of the same device. They then queued on
        the single radio and dragged each other out from 28 s to 74 s — the
        pile-up was self-inflicted, and it grew with the number of viewers.
        """
        from backend.app.services.zigbee.reporting import refresh_plug

        device = self._device_for(plug)
        if device is None:
            return False

        task = self._refreshing.get(plug.id)
        if task is None or task.done():
            task = asyncio.create_task(refresh_plug(self, plug, device), name=f"zigbee_refresh_{plug.id}")
            self._refreshing[plug.id] = task
            task.add_done_callback(self._forget_refresh)

        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except TimeoutError:
            logger.debug(
                "Zigbee plug %s: read still running after %.1fs, answering from what we have", plug.id, timeout
            )
            return False
        except Exception as exc:  # noqa: BLE001 — a read that failed is not a reading
            logger.warning("Zigbee plug %s: refresh failed: %s", plug.id, describe_exception(exc))
            return False

    async def teardown(self, plug_id: int) -> None:
        """Forget a plug that has been deleted.

        Without this the shared refresh task keeps running, ``_refreshing``
        keeps an entry for a row that no longer exists, and the report listeners
        stay bound to the device's clusters — so the radio, the scarcest thing
        in this subsystem, goes on being spent on a plug BamDude no longer
        manages. Measured in the field: reads of a deleted plug continued for
        eleven seconds past the deletion of its row.

        The counterpart of the MQTT branch's ``unsubscribe`` in the same delete
        handler, which is what made the omission easy to miss.
        """
        task = self._refreshing.pop(plug_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for key in [k for k in self._listeners if k[0] == plug_id]:
            self._listeners.pop(key, None)
        self._cache.pop(plug_id, None)
        # Or the next plug to be given this id inherits a threshold nobody set
        # for it — plug ids are reused, and this one is invisible when wrong.
        self._stale_after.pop(plug_id, None)

    async def get_status(self, plug: Any) -> dict:
        """Current state, refreshed by a direct read if the cache has aged out.

        A device that is present but has never reported reads as unreachable.
        That is honest rather than pessimistic — we genuinely do not know its
        state — so the seconds after binding are not a fault.
        """
        unknown = {"state": None, "reachable": False, "device_name": None}
        if self._device_for(plug) is None:
            return unknown

        data = self.get_plug_data(plug.id)
        if data is None or self._is_stale(data, plug.id):
            if not await self.refresh(plug, timeout=_REQUEST_REFRESH_BUDGET_SECONDS):
                return unknown
            data = self.get_plug_data(plug.id)
            if data is None:
                return unknown

        return {"state": data.state, "reachable": True, "device_name": None}

    async def get_energy(self, plug: Any) -> dict | None:
        """Energy readings, in kWh and watts.

        ``total`` is the lifetime counter and the only key that may feed
        ``smart_plug_energy_snapshots``. It is **omitted** when unknown rather
        than sent as zero: a zero would be differenced by the per-print
        machinery as real consumption, which is the wrong-rather-than-missing
        failure this whole path is built to avoid.
        """
        data = self.get_plug_data(plug.id)
        if data is not None and self._is_stale(data, plug.id):
            # Refresh rather than hand back an aged reading. If the refresh
            # fails, report nothing: a stale wattage is indistinguishable from a
            # live one to every consumer downstream.
            if await self.refresh(plug, timeout=_REQUEST_REFRESH_BUDGET_SECONDS):
                data = self.get_plug_data(plug.id)
            else:
                return None
        if data is None:
            return None

        energy: dict[str, float] = {}
        if data.state == "OFF":
            # The relay is open, so the load draws nothing. Stated here as
            # physics rather than trusted from the device, because plugs lie
            # about exactly this: ours keeps answering with the last wattage it
            # measured, so a socket with nothing running reported 33 W.
            #
            # The quirk for that model does zero the reading, but only on a
            # transition it witnesses — its OnOff half writes 0 into
            # ``active_power`` while its ElectricalMeasurement half blocks every
            # write to ``active_power`` once the socket reads off, so the zero
            # only lands because the on/off cache still says ON at that instant.
            # After a restart, with a wattage restored from zigpy's database and
            # no transition to witness, nothing corrects it. This rule needs no
            # transition, and needs no quirk to exist for the plug at hand.
            energy["power"] = 0.0
        elif data.power is not None:
            energy["power"] = data.power
        if data.energy_total is not None:
            energy["total"] = data.energy_total
        return energy

    # ---- capability probe, used when binding --------------------------------

    def capabilities(self, plug: Any) -> dict[str, bool]:
        """Which clusters this plug actually has, for callers that need to know
        whether energy will ever arrive rather than waiting for a zero."""
        device = self._device_for(plug)
        return {
            "on_off": self._cluster(device, ON_OFF) is not None,
            "metering": self._cluster(device, METERING) is not None,
            "electrical_measurement": self._cluster(device, ELECTRICAL_MEASUREMENT) is not None,
        }


zigbee_smart_plug_service = ZigbeeSmartPlugService()
