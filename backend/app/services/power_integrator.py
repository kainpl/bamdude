"""The one integrator of plug power history (spec plug-meter-watchdog W3, W4).

Pure: samples in, energy and its quality out, no I/O. The meter-stall detector and the archive
estimate both read history through here, so what counts as a usable moment, a covered stretch and
enough history is decided once.

* **One moment, one point.** Timestamps are brought to UTC first (a naive one means UTC — that is
  what the history columns hold), then every sample of the same moment becomes one point. A moment
  with any reason is unusable even when a valid duplicate stands beside it; two different valid
  values are a conflict, never a choice.
* **Segments, never carry-forward.** Between two usable points no further apart than the gap limit
  the energy is a trapezoid, clipped to the window by linear interpolation. A longer gap, or a
  segment touching an unusable point, contributes nothing — neither energy nor coverage. Nothing is
  extrapolated past the first or the last point, and nothing is rescaled to the whole window.
* **Insufficient is not zero.** The result says whether the history is good enough; when it is not,
  the energy beside it is a diagnostic, never a figure for an archive.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

#: Coverage below which history is not enough (spec W4).
MIN_COVERAGE = 0.95
#: Joules per kWh -- a W x s product divided by this is kWh.
_WS_PER_KWH = 3_600_000.0
_CONFLICT = "conflict"
_INVALID = "invalid_input"


@dataclass(frozen=True)
class Sample:
    """One received power reading. ``reason`` set means it was received and found unusable."""

    at: datetime
    watts: float
    reason: str | None = None


@dataclass(frozen=True)
class Point:
    at: datetime  # aware UTC
    watts: float | None  # None when unusable
    reason: str | None

    @property
    def usable(self) -> bool:
        return self.reason is None


@dataclass(frozen=True)
class Integration:
    kwh: float
    covered_seconds: float
    window_seconds: float
    max_gap: float
    samples: int
    head_gap: float
    tail_gap: float
    reasons: tuple[str, ...]

    @property
    def coverage(self) -> float:
        return self.covered_seconds / self.window_seconds if self.window_seconds > 0 else 0.0

    @property
    def sufficient(self) -> bool:
        return not self.reasons


def as_utc(moment: datetime) -> datetime:
    """A naive timestamp is UTC (the history columns hold UTC without a zone); an aware one is converted."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _valid_watts(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    watts = float(value)
    if not math.isfinite(watts) or watts < 0:
        return None
    return watts


def normalize(samples: Iterable[Sample]) -> list[Point]:
    """Sorted points, one per UTC moment (spec W3: a reason wins; W4: conflicting values are unusable)."""
    by_moment: dict[datetime, list[Sample]] = {}
    for sample in samples:
        by_moment.setdefault(as_utc(sample.at), []).append(sample)
    points = []
    for moment in sorted(by_moment):
        group = by_moment[moment]
        reasons = sorted({s.reason for s in group if s.reason is not None})
        if reasons:
            points.append(Point(moment, None, reasons[0]))
            continue
        values = {_valid_watts(s.watts) for s in group}
        if None in values:
            points.append(Point(moment, None, _INVALID))
        elif len(values) > 1:
            points.append(Point(moment, None, _CONFLICT))
        else:
            points.append(Point(moment, values.pop(), None))
    return points


def integrate(
    points: Sequence[Point],
    start: datetime,
    end: datetime,
    *,
    gap_limit: float,
    edge_limit: float,
    min_coverage: float = MIN_COVERAGE,
) -> Integration:
    """Energy of ``points`` over [start, end] and whether the history is enough to state it."""
    start, end = as_utc(start), as_utc(end)
    window = max((end - start).total_seconds(), 0.0)
    ws = covered = max_gap = 0.0
    first_covered = last_covered = None
    inside = 0
    for p0, p1 in zip(points, points[1:], strict=False):
        dt = (p1.at - p0.at).total_seconds()
        if p1.at > start and p0.at < end:
            max_gap = max(max_gap, dt)
        if not (p0.usable and p1.usable) or dt <= 0 or dt > gap_limit:
            continue
        a, b = max(p0.at, start), min(p1.at, end)
        if b <= a:
            continue
        slope = (p1.watts - p0.watts) / dt
        wa = p0.watts + slope * (a - p0.at).total_seconds()
        wb = p0.watts + slope * (b - p0.at).total_seconds()
        seconds = (b - a).total_seconds()
        ws += (wa + wb) / 2 * seconds
        covered += seconds
        first_covered = a if first_covered is None else min(first_covered, a)
        last_covered = b if last_covered is None else max(last_covered, b)
    inside = sum(1 for p in points if start <= p.at <= end)

    reasons: list[str] = []
    if first_covered is None:
        head = tail = window
        reasons.append("no_samples" if inside == 0 else "coverage")
    else:
        head = (first_covered - start).total_seconds()
        tail = (end - last_covered).total_seconds()
        if window <= 0 or covered / window < min_coverage - 1e-12:
            reasons.append("coverage")
        if head > edge_limit:
            reasons.append("edge_head")
        if tail > edge_limit:
            reasons.append("edge_tail")
    return Integration(
        kwh=ws / _WS_PER_KWH,
        covered_seconds=covered,
        window_seconds=window,
        max_gap=max_gap,
        samples=inside,
        head_gap=head,
        tail_gap=tail,
        reasons=tuple(reasons),
    )
