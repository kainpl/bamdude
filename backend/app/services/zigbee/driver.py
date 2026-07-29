"""The Zigbee smart-plug driver.

Implements the same four methods every other plug type does — ``get_status``,
``turn_on``, ``turn_off``, ``get_energy``, plus ``toggle`` — so
``SmartPlugManager`` reaches it with one branch and everything already built on
that interface follows: the control endpoint, the scheduler, auto-off after a
print, and Obico's "pause and cut power".

State and readings come from a **cache fed by attribute reporting**, not from
polling. Polling a mesh on a shared radio costs airtime that scales with the
number of plugs, and the device already knows how to push. The shape is
deliberately the same as ``mqtt_smart_plug.py``'s subscription cache rather than
a second pattern invented alongside it.

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

from backend.app.services.zigbee.coordinator import zigbee_coordinator
from backend.app.services.zigbee.devices import ELECTRICAL_MEASUREMENT, METERING, ON_OFF

logger = logging.getLogger(__name__)

# On/Off cluster command ids (ZCL). Named rather than inlined — a bare 0 and 1
# in a switch call is exactly what gets transposed in a later edit.
_CMD_OFF = 0x00
_CMD_ON = 0x01


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
            await cluster.command(command_id)
        except Exception as exc:  # noqa: BLE001 — a switch failure is reported, never raised
            # The cache is deliberately untouched: the device's own report is
            # what updates state. Writing the state we hoped for is how
            # automation comes to believe a printer is powered when it is not.
            logger.warning("Zigbee plug %s: %s failed: %s", plug.id, what, exc)
            return False
        return True

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

    async def get_status(self, plug: Any) -> dict:
        """Current state.

        A device that is present but has not reported yet reads as unreachable.
        That is honest rather than pessimistic — we genuinely do not know its
        state — but it means the seconds after binding are not a fault.
        """
        data = self.get_plug_data(plug.id)
        if data is None or self._device_for(plug) is None:
            return {"state": None, "reachable": False, "device_name": None}
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
        if data is None:
            return None

        energy: dict[str, float] = {}
        if data.power is not None:
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
