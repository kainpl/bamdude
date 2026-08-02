"""Sensor readings, and the freshness rule that is not the plugs' rule.

A plug is mains-powered and answers when asked, so its freshness rests on a
poll — that is this subsystem's existing invariant and it stays true for plugs.
A battery sensor is asleep almost always: a request reaches it only when it
polls its parent, and the parent holds that request for roughly 7.7 s. Poll such
a device every 30 s and every attempt times out while holding the one radio, and
the cell is flat in weeks.

So the mechanism is chosen per device from its node descriptor, not per class —
which also means an AirGuard on USB is polled exactly like a plug, because it
genuinely can be.

Everything here is in memory. This cycle creates no rows: after a restart
nothing is known, which the startup read and the watchdog then repair.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from backend.app.services.zigbee.measurements import BY_KEY, to_display

logger = logging.getLogger(__name__)

# How many consecutive windows may pass with nothing heard before a sensor is
# called unreachable. One failed read proves nothing about a sleeper: a healthy
# one can simply have missed the few seconds in which its parent held the
# request.
EMPTY_WINDOWS_BEFORE_UNREACHABLE = 3


class PowerClass(str, Enum):
    MAINS = "mains"
    BATTERY = "battery"


def power_class(device) -> PowerClass:
    """Mains or battery, from the node descriptor's RxOnWhenIdle bit.

    An unknown descriptor is treated as battery. That is the safe way round:
    polling a sleeper wastes radio and battery for nothing, while declining to
    poll a mains device only costs some freshness that its reports will supply.
    """
    node_desc = getattr(device, "node_desc", None)
    flags = getattr(node_desc, "mac_capability_flags", None)
    rx_on = getattr(flags, "RxOnWhenIdle", None)
    if rx_on is None:
        return PowerClass.BATTERY
    return PowerClass.MAINS if bool(rx_on) else PowerClass.BATTERY


@dataclass(frozen=True)
class Reading:
    """One quantity as last heard.

    ``value`` is None when the device reported something that is not a
    measurement — which is still contact, and a different fact from silence.
    """

    value: float | None
    unit: str
    at: datetime


@dataclass
class _SensorState:
    readings: dict[str, Reading] = field(default_factory=dict)
    last_attempt_at: datetime | None = None
    empty_windows: int = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SensorStore:
    """Everything known about the sensors, held in memory."""

    def __init__(self) -> None:
        self._state: dict[str, _SensorState] = {}

    @staticmethod
    def _key(ieee: str) -> str:
        return str(ieee).strip().lower()

    def record(self, ieee: str, key: str, raw, now: datetime | None = None) -> None:
        """Take one attribute value from a report or a read.

        Contact is recorded even when the value is unusable: the device spoke,
        so the empty-window count resets and the reading is not stale — it is
        simply empty. A key outside the registry is noise and is dropped;
        devices report far more than they were asked for.
        """
        measurement = BY_KEY.get(key)
        if measurement is None:
            return
        state = self._state.setdefault(self._key(ieee), _SensorState())
        state.readings[key] = Reading(
            value=to_display(measurement, raw),
            unit=measurement.unit,
            at=now or _now(),
        )
        state.empty_windows = 0

    def reading(self, ieee: str, key: str) -> Reading | None:
        state = self._state.get(self._key(ieee))
        return state.readings.get(key) if state else None

    def is_stale(self, ieee: str, key: str, max_interval: int, multiplier: float, now: datetime | None = None) -> bool:
        """Older than its window, or never heard at all."""
        reading = self.reading(ieee, key)
        if reading is None:
            return True
        return ((now or _now()) - reading.at).total_seconds() > max_interval * multiplier

    def due_for_watchdog(self, ieee: str, key: str, window: float, now: datetime | None = None) -> bool:
        """Whether this sensor has earned its one read for this window.

        Two conditions, both required: nothing heard for a whole window, and no
        attempt already made inside it. Without the second, every cycle would
        read a device that is asleep by definition.
        """
        moment = now or _now()
        reading = self.reading(ieee, key)
        quiet_for = float("inf") if reading is None else (moment - reading.at).total_seconds()
        if quiet_for <= window:
            return False
        state = self._state.get(self._key(ieee))
        if state is None or state.last_attempt_at is None:
            return True
        return (moment - state.last_attempt_at).total_seconds() > window

    def note_attempt(self, ieee: str, now: datetime | None = None) -> None:
        """A read was attempted and nothing came back. Counts the window."""
        state = self._state.setdefault(self._key(ieee), _SensorState())
        state.last_attempt_at = now or _now()
        state.empty_windows += 1

    def note_success(self, ieee: str) -> None:
        state = self._state.setdefault(self._key(ieee), _SensorState())
        state.empty_windows = 0

    def empty_windows(self, ieee: str) -> int:
        state = self._state.get(self._key(ieee))
        return state.empty_windows if state else 0

    def is_unreachable(self, ieee: str) -> bool:
        return self.empty_windows(ieee) >= EMPTY_WINDOWS_BEFORE_UNREACHABLE

    def known_ieees(self) -> tuple[str, ...]:
        return tuple(self._state)

    def forget(self, ieee: str) -> None:
        """Drop everything about a sensor that has been unpaired."""
        self._state.pop(self._key(ieee), None)


sensor_store = SensorStore()
