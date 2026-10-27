"""The meter-stall detector (spec plug-meter-watchdog W1, W5).

A **suspicion**, never a verdict about the device: a plug's lifetime counter held one value for at
least ``MIN_FLAT`` while the power history of the same source covers that stretch well
(``power_integrator``) and adds up to at least the expected energy. Two channels contradicting each
other is all it shows; a stale cache and a frozen MCU look the same from here.

Pure. The counter points are what BamDude read (hourly snapshots and the ends of prints), the power
points what the plug reported; the caller has already restricted both to one measurement source.

* An increase breaks a flat series and, after a suspicious one, is the recovery.
* A decrease is a reset: a new baseline, never evidence of a stall, never a recovery by itself.
* Snapshots further apart than ``MAX_SNAPSHOT_GAP`` split a series: what happened between them is
  unknown, not flat.
* A zero delta over a short or a light print is no signal (3 min x 120 W = 6 Wh fits inside one
  10 Wh step of the counter that started this).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from backend.app.services.power_integrator import Integration, Point, as_utc, integrate

#: Shortest flat stretch that may become a suspicion.
MIN_FLAT = timedelta(minutes=30)
#: Default energy the history must show over a flat stretch (Wh). A heuristic for the known 10 Wh
#: counter step, not a metrological guarantee -- a plug may set a larger one.
DEFAULT_MIN_WH = 100.0
#: Snapshots further apart prove nothing about the stretch between them.
MAX_SNAPSHOT_GAP = timedelta(hours=2)
#: Only float noise -- never a counter's step.
_TOLERANCE_KWH = 1e-9


@dataclass(frozen=True)
class CounterPoint:
    at: datetime
    total_kwh: float


@dataclass(frozen=True)
class FlatSeries:
    start: datetime
    end: datetime
    total_kwh: float
    integral: Integration


@dataclass
class Detection:
    #: moving / suspicious / reset / insufficient / idle -- what the LAST stretch says.
    state: str
    suspicious: list[FlatSeries] = field(default_factory=list)
    #: The first increase after the last suspicious series, when there is one.
    recovered_at: datetime | None = None


def _runs(points: Sequence[CounterPoint]):
    """Maximal flat runs, each ended by what broke it: ``up``, ``down``, ``gap`` or ``end``."""
    if not points:
        return
    run = [points[0]]
    for point in points[1:]:
        last = run[-1]
        delta = point.total_kwh - last.total_kwh
        if point.at - last.at > MAX_SNAPSHOT_GAP:
            yield run, "gap", point
        elif delta > _TOLERANCE_KWH:
            yield run, "up", point
        elif delta < -_TOLERANCE_KWH:
            yield run, "down", point
        else:
            run.append(point)
            continue
        run = [point]
    yield run, "end", None


def detect(
    counters: Sequence[CounterPoint],
    power: Sequence[Point],
    *,
    gap_limit: float,
    edge_limit: float,
    min_wh: float = DEFAULT_MIN_WH,
) -> Detection:
    """Every proven flat series, and the state the last stretch leaves the counter in."""
    points = sorted((CounterPoint(as_utc(c.at), float(c.total_kwh)) for c in counters), key=lambda c: c.at)
    detection = Detection(state="idle")
    previous_break = None  # what ended the run before the current one
    awaiting_recovery = False
    for run, broken_by, breaker in _runs(points):
        start, end = run[0].at, run[-1].at
        run_state = "short"
        if len(run) >= 2 and end - start >= MIN_FLAT:
            integral = integrate(power, start, end, gap_limit=gap_limit, edge_limit=edge_limit)
            if not integral.sufficient:
                run_state = "insufficient"
            elif integral.kwh * 1000.0 >= min_wh:
                detection.suspicious.append(FlatSeries(start, end, run[0].total_kwh, integral))
                detection.recovered_at = None
                awaiting_recovery = True
                run_state = "suspicious"
        if broken_by == "up" and awaiting_recovery:
            detection.recovered_at = breaker.at
            awaiting_recovery = False
        if broken_by == "end":
            if run_state in ("suspicious", "insufficient"):
                detection.state = run_state
            else:
                detection.state = {"up": "moving", "down": "reset"}.get(previous_break, "idle")
        previous_break = broken_by
    return detection
