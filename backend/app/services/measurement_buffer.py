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

Since m211 (spec plug-meter-watchdog W3, W10) a power reading also carries:

* the **source token** its producer captured, so the flush can drop a reading
  of a configuration the plug no longer has;
* a **reason** when it was received and found unusable — a known OFF becomes
  0 W, an unknown relay state on a plug that may repeat stale watts becomes
  ``state_unknown``, a value that is no number ``invalid_input``. Kept, never
  dropped: two good neighbours must not be joined across it.

And the hand-off to the database is visible: ``take_power`` moves the pending
readings into an **in-flight** batch that readers still see (``peek_power``)
until the flush acknowledges its commit. A rollback known to have happened puts
the batch back; a commit whose outcome is unknown turns it into
``batch_unknown`` markers, one per reading, so the moment stays unusable rather
than silently empty. Overflow drops the OLDEST readings and keeps where they
were, per plug and source, as ``buffer_loss`` markers. One short thread lock,
no await under it: MQTT readings arrive on paho's thread.

Overflow never drops a ``batch_unknown`` marker (review r1 MW-R3): its commit
may have landed, so the database may hold a valid copy of that moment, and only
the marker beside it keeps the copy unusable -- two edge markers of a loss span
would leave every copy between them usable again. The markers wait in their own
queue and newer readings give way to them instead. Memory stays bounded: the
markers come from one in-flight batch, which never holds more than the buffer.
"""

from __future__ import annotations

import logging
import math
import threading
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# A ceiling, not a target. The drain can stop — a database outage, a cancelled
# task — and when it does, memory must not become the next thing to fail. At the
# farm's normal rate this is roughly half an hour of readings.
MAX_BUFFERED = 20_000

#: Why a received reading is unusable (spec W3). Any other non-NULL value is unusable too.
STATE_UNKNOWN = "state_unknown"
INVALID_INPUT = "invalid_input"
BATCH_UNKNOWN = "batch_unknown"
BUFFER_LOSS = "buffer_loss"


@dataclass(frozen=True)
class PowerSample:
    plug_id: int
    power: float
    recorded_at: datetime
    source_token: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class SensorSample:
    ieee: str
    kind: str
    value: float
    recorded_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _valid_watts(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        watts = float(value)
    except (TypeError, ValueError):
        return None
    return watts if math.isfinite(watts) and watts >= 0 else None


class MeasurementBuffer:
    """Readings waiting to be written. Appending is safe from any callback and any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._power: deque[PowerSample] = deque()
        # Markers of a commit whose outcome is unknown: never evicted (see the module docstring).
        self._unknown: deque[PowerSample] = deque()
        # deque with maxlen drops from the LEFT when full, keeping the newest —
        # which is the right end to lose: an old reading nobody has drawn yet
        # matters less than the one describing what is happening now.
        self._sensors: deque[SensorSample] = deque(maxlen=MAX_BUFFERED)
        self._in_flight: list[PowerSample] = []
        # (plug, token) -> [first, last] moment of readings dropped by overflow. Oldest-first
        # dropping makes them a contiguous run with nothing of that plug between: two markers say it.
        self._loss: dict[tuple[int, str | None], list[datetime]] = {}
        self._retired = False

    # ---- producers ------------------------------------------------------------

    def record_power(
        self,
        plug_id: int,
        watts,
        *,
        source_token: str | None = None,
        state: str | None = None,
        phantom_risk: bool = False,
    ) -> None:
        """One plug reading. Never raises — see the module docstring.

        ``state`` is the relay as known AT this reading ("ON", "OFF" or None). A known OFF is 0 W
        whatever the firmware repeats; an unknown state makes the reading unusable only for a plug
        whose watts may outlive its relay (``phantom_risk``). No value at all is no reading.
        """
        try:
            if watts is None:
                return
            value = _valid_watts(watts)
            reason = None
            if state == "OFF":
                value, reason = 0.0, None
            elif value is None:
                value, reason = 0.0, INVALID_INPUT
            elif phantom_risk and state != "ON":
                reason = STATE_UNKNOWN
            sample = PowerSample(int(plug_id), value, _now(), source_token, reason)
            with self._lock:
                if self._retired:
                    return
                self._append(sample)
        except Exception as exc:  # noqa: BLE001 — a callback must not raise
            logger.debug("Measurement buffer: power reading ignored: %s", exc)

    def record_sensor(self, ieee: str, kind: str, value) -> None:
        """One sensor reading. Never raises — see the module docstring."""
        try:
            if value is None or self._retired:
                return
            self._sensors.append(SensorSample(ieee=str(ieee), kind=str(kind), value=float(value), recorded_at=_now()))
        except Exception as exc:  # noqa: BLE001 — a callback must not raise
            logger.debug("Measurement buffer: sensor reading ignored: %s", exc)

    def _append(self, sample: PowerSample) -> None:
        """Keep ``sample`` within the bound; what gives way is the oldest ordinary reading, never a marker
        of an unknown commit -- and when only those markers are left, the new reading itself."""
        while len(self._unknown) + len(self._power) >= MAX_BUFFERED and self._power:
            self._lose(self._power.popleft())
        if len(self._unknown) + len(self._power) >= MAX_BUFFERED:
            self._lose(sample)
            return
        self._power.append(sample)

    def _lose(self, dropped: PowerSample) -> None:
        span = self._loss.setdefault((dropped.plug_id, dropped.source_token), [dropped.recorded_at] * 2)
        span[0] = min(span[0], dropped.recorded_at)
        span[1] = max(span[1], dropped.recorded_at)

    def _trim(self) -> None:
        while len(self._unknown) + len(self._power) > MAX_BUFFERED and self._power:
            self._lose(self._power.popleft())

    def _loss_markers(self) -> list[PowerSample]:
        markers = []
        for (plug_id, token), (first, last) in self._loss.items():
            for moment in {first, last}:
                markers.append(PowerSample(plug_id, 0.0, moment, token, BUFFER_LOSS))
        return markers

    # ---- the flush's hand-off -------------------------------------------------

    def take_power(self) -> list[PowerSample]:
        """Move everything pending into the in-flight batch the flush is about to write."""
        with self._lock:
            if self._in_flight:  # one flush at a time; the previous batch is still unanswered
                return []
            batch = list(self._unknown) + list(self._power) + self._loss_markers()
            self._unknown.clear()
            self._power.clear()
            self._loss.clear()
            self._in_flight = batch
            return list(batch)

    def ack_power(self) -> None:
        """The in-flight batch is committed."""
        with self._lock:
            self._in_flight = []

    def return_power(self) -> None:
        """The in-flight batch is known NOT to be written: it goes back in front of the newer readings."""
        with self._lock:
            self._requeue(self._in_flight)

    def mark_unknown(self) -> None:
        """The in-flight batch's commit has an unknown outcome: each reading's moment becomes a marker."""
        with self._lock:
            self._requeue(
                [s if s.reason == BUFFER_LOSS else replace(s, power=0.0, reason=BATCH_UNKNOWN) for s in self._in_flight]
            )

    def _requeue(self, older: list[PowerSample]) -> None:
        """Put a batch back: its unknown-commit markers into their own queue, its loss edges back into their
        spans, its readings in front of the newer ones -- then the oldest ordinary readings give way."""
        self._in_flight = []
        readings = []
        for sample in older:
            if sample.reason == BATCH_UNKNOWN:
                self._unknown.append(sample)
            elif sample.reason == BUFFER_LOSS:
                self._lose(sample)
            else:
                readings.append(sample)
        self._power.extendleft(reversed(readings))
        self._trim()

    # ---- readers --------------------------------------------------------------

    def peek_power(self, plug_id: int) -> tuple[list[PowerSample], bool]:
        """This plug's readings not yet known to be in the database, and whether the buffer was retired.

        Pending, in flight and overflow markers -- copied, never consumed. A reader copies these FIRST
        and only then opens a fresh database read: a reading is then in one of the two, or in both.
        """
        with self._lock:
            pending = [s for s in self._unknown if s.plug_id == plug_id]
            pending += [s for s in self._power if s.plug_id == plug_id]
            flying = [s for s in self._in_flight if s.plug_id == plug_id]
            lost = [m for m in self._loss_markers() if m.plug_id == plug_id]
            return pending + flying + lost, self._retired

    def drain_sensors(self) -> list[SensorSample]:
        with self._lock:
            sensors = list(self._sensors)
            self._sensors.clear()
            return sensors

    def drain(self) -> tuple[list[PowerSample], list[SensorSample]]:
        """Take everything waiting and empty the buffer (tests, and code that owns no hand-off).

        One call, both lists: a flush that took one and left the other would
        write them at different times and make the two histories disagree about
        when the process last ran.
        """
        with self._lock:
            power = list(self._unknown) + list(self._power) + self._loss_markers()
            self._unknown.clear()
            self._power.clear()
            self._loss.clear()
            self._in_flight = []
            sensors = list(self._sensors)
            self._sensors.clear()
            return power, sensors

    def retire(self) -> None:
        """A restore replaced the storage: nothing buffered belongs to it, and nothing more is taken.

        A restart follows every restore, and it brings a fresh buffer.
        """
        with self._lock:
            self._unknown.clear()
            self._power.clear()
            self._sensors.clear()
            self._in_flight = []
            self._loss.clear()
            self._retired = True

    def pending(self) -> int:
        return len(self._unknown) + len(self._power) + len(self._sensors)

    @property
    def retired(self) -> bool:
        return self._retired


measurement_buffer = MeasurementBuffer()
