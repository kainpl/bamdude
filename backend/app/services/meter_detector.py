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
* A hole in old history does not hide a proven stretch (review r1 MW-R1): a flat run is proven when
  ANY stretch of it between two of its counter points -- at least ``MIN_FLAT`` long -- is covered
  well enough and shows the energy. The integrals are taken once per pair of neighbouring counter
  points over a slice of the history, and the stretches are compared through prefix sums, so a day
  of hourly snapshots costs one pass over the history, not one per candidate.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from backend.app.services.power_integrator import MIN_COVERAGE, Integration, Point, as_utc, integrate

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
    """A flat run of the counter (``start``..``end``) and the integral that proves it -- over the whole
    run when the history covers it, else over the longest proven stretch inside it."""

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


class _History:
    """The power points with their moments, so a window is integrated over its own slice."""

    def __init__(self, power: Sequence[Point]) -> None:
        self.points = list(power)
        self.times = [p.at for p in self.points]

    def integrate(self, start: datetime, end: datetime, *, gap_limit: float, edge_limit: float) -> Integration:
        # One point either side of the window: the segments crossing its edges are clipped, the rest
        # contribute nothing, so the slice gives exactly what the whole history would.
        lo = max(bisect_left(self.times, start) - 1, 0)
        hi = min(bisect_right(self.times, end) + 1, len(self.points))
        return integrate(self.points[lo:hi], start, end, gap_limit=gap_limit, edge_limit=edge_limit)


def _proven_stretch(run, history: _History, *, gap_limit: float, edge_limit: float):
    """The longest stretch of ``run`` the history covers well enough, as (start, end, energy kWh).

    Coverage, energy and the edges are added up per pair of neighbouring counter points -- exactly
    what one integral over the stretch gives, because the integrator clips segments linearly at the
    window's edges. None when no stretch of at least ``MIN_FLAT`` is covered.
    """
    times = [c.at for c in run]
    parts = [
        history.integrate(a, b, gap_limit=gap_limit, edge_limit=edge_limit)
        for a, b in zip(times, times[1:], strict=False)
    ]
    covered = [0.0]
    energy = [0.0]
    for part in parts:
        covered.append(covered[-1] + part.covered_seconds)
        energy.append(energy[-1] + part.kwh)
    # The nearest covered part at or after / at or before each index: the edges of a stretch.
    next_covered: list[int | None] = [None] * (len(parts) + 1)
    for m in range(len(parts) - 1, -1, -1):
        next_covered[m] = m if parts[m].covered_seconds > 0 else next_covered[m + 1]
    prev_covered: list[int | None] = [None] * len(parts)
    for m in range(len(parts)):
        prev_covered[m] = m if parts[m].covered_seconds > 0 else (prev_covered[m - 1] if m else None)
    best = None
    for i in range(len(parts)):
        first = next_covered[i]
        if first is None:
            break
        if (times[first] - times[i]).total_seconds() + parts[first].head_gap > edge_limit:
            continue
        for j in range(len(parts), i, -1):  # the longest stretch from i first
            span = (times[j] - times[i]).total_seconds()
            if span < MIN_FLAT.total_seconds():
                break
            last = prev_covered[j - 1]
            if last is None or last < i:
                continue
            tail = (times[j] - times[last + 1]).total_seconds() + parts[last].tail_gap
            if tail > edge_limit or (covered[j] - covered[i]) / span < MIN_COVERAGE - 1e-12:
                continue
            if best is None or span > best[3] or (span == best[3] and times[j] > best[1]):
                best = (times[i], times[j], energy[j] - energy[i], span)
            break
    return None if best is None else best[:3]


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
    history = _History(power)
    detection = Detection(state="idle")
    previous_break = None  # what ended the run before the current one
    awaiting_recovery = False
    for run, broken_by, breaker in _runs(points):
        start, end = run[0].at, run[-1].at
        run_state = "short"
        if len(run) >= 2 and end - start >= MIN_FLAT:
            proven = _proven_stretch(run, history, gap_limit=gap_limit, edge_limit=edge_limit)
            if proven is None:
                run_state = "insufficient"
            elif proven[2] * 1000.0 >= min_wh:
                integral = history.integrate(proven[0], proven[1], gap_limit=gap_limit, edge_limit=edge_limit)
                if integral.sufficient:  # the sums agree with one integral; never trust them over it
                    detection.suspicious.append(FlatSeries(start, end, run[0].total_kwh, integral))
                    detection.recovered_at = None
                    awaiting_recovery = True
                    run_state = "suspicious"
                else:
                    run_state = "insufficient"
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
