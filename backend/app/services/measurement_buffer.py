"""What sits between a device callback and the database.

Report handlers are synchronous by necessity: an ``async def`` listener returns
a coroutine nobody runs, so every report would vanish. They also must not raise
— zigpy logs a listener exception at DEBUG and carries on, so a failed database
write there would not even be visible.

So the append is synchronous and cannot fail, and the flush belongs to the loop
that has to exist anyway for the plug types that never report. Every reading
still becomes a row; only the moment of writing moves.

The cost, stated rather than hidden: up to one flush interval of readings is
lost if the process dies. For a chart that is a gap of at most a minute, and the
alternative is I/O inside a callback that cannot report its own failure.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# A ceiling, not a target. The drain can stop — a database outage, a cancelled
# task — and when it does, memory must not become the next thing to fail. At the
# farm's normal rate this is roughly half an hour of readings.
MAX_BUFFERED = 20_000


@dataclass(frozen=True)
class PowerSample:
    plug_id: int
    power: float
    recorded_at: datetime


@dataclass(frozen=True)
class SensorSample:
    ieee: str
    kind: str
    value: float
    recorded_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MeasurementBuffer:
    """Readings waiting to be written. Appending is safe from any callback."""

    def __init__(self) -> None:
        # deque with maxlen drops from the LEFT when full, keeping the newest —
        # which is the right end to lose: an old reading nobody has drawn yet
        # matters less than the one describing what is happening now.
        self._power: deque[PowerSample] = deque(maxlen=MAX_BUFFERED)
        self._sensors: deque[SensorSample] = deque(maxlen=MAX_BUFFERED)

    def record_power(self, plug_id: int, watts) -> None:
        """One plug reading. Never raises — see the module docstring."""
        try:
            if watts is None:
                return
            self._power.append(PowerSample(plug_id=int(plug_id), power=float(watts), recorded_at=_now()))
        except Exception as exc:  # noqa: BLE001 — a callback must not raise
            logger.debug("Measurement buffer: power reading ignored: %s", exc)

    def record_sensor(self, ieee: str, kind: str, value) -> None:
        """One sensor reading. Never raises — see the module docstring."""
        try:
            if value is None:
                return
            self._sensors.append(SensorSample(ieee=str(ieee), kind=str(kind), value=float(value), recorded_at=_now()))
        except Exception as exc:  # noqa: BLE001 — a callback must not raise
            logger.debug("Measurement buffer: sensor reading ignored: %s", exc)

    def drain(self) -> tuple[list[PowerSample], list[SensorSample]]:
        """Take everything waiting and empty the buffer.

        One call, both lists: a flush that took one and left the other would
        write them at different times and make the two histories disagree about
        when the process last ran.
        """
        power, sensors = list(self._power), list(self._sensors)
        self._power.clear()
        self._sensors.clear()
        return power, sensors

    def pending(self) -> int:
        return len(self._power) + len(self._sensors)


measurement_buffer = MeasurementBuffer()
