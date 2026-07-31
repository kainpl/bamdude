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

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from zigpy.zcl import foundation

from backend.app.services.zigbee.coordinator import CoordinatorState, zigbee_coordinator
from backend.app.services.zigbee.devices import ELECTRICAL_MEASUREMENT, METERING, ON_OFF

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
    def __init__(self):
        self._cache: dict[int, ZigbeePlugData] = {}
        # Listeners built at bind time, kept so an on-demand read can reuse one
        # instead of rebuilding it. They carry the cluster's multiplier/divisor,
        # which are device constants — re-reading them per refresh would put two
        # mesh round-trips behind a number that never changes.
        self._listeners: dict[tuple[int, int], Any] = {}

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
            logger.warning("Zigbee plug %s: %s failed: %s", plug.id, what, exc)
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

    def _is_stale(self, data) -> bool:
        last_seen = getattr(data, "last_seen", None)
        if last_seen is None:
            return True
        return (datetime.now(timezone.utc) - last_seen).total_seconds() > _STALE_AFTER_SECONDS

    async def refresh(self, plug: Any) -> bool:
        """Read every subscribed attribute from the device, now.

        Both the background poller and the staleness path below go through here,
        so there is one way a reading enters the cache from a read.
        """
        from backend.app.services.zigbee.reporting import refresh_plug

        device = self._device_for(plug)
        if device is None:
            return False
        return await refresh_plug(self, plug, device)

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
        if data is None or self._is_stale(data):
            if not await self.refresh(plug):
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
        if data is not None and self._is_stale(data):
            # Refresh rather than hand back an aged reading. If the refresh
            # fails, report nothing: a stale wattage is indistinguishable from a
            # live one to every consumer downstream.
            if await self.refresh(plug):
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
