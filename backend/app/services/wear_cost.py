"""Wear cost of a print: the printer's rate per printing hour times the measured duration.

This module is the ONE writer of ``print_archives.wear_cost`` / ``wear_rate``
(``tests/unit/test_wear_cost_has_one_writer.py`` scans the tree). The figure is a fact
of the run: it is stamped once, where the archive receives its terminal status from a
MEASURED end, with the rate the printer had at that moment, and it is never recomputed
-- a later rate change moves no stored figure and there is no backfill.

No figure is NULL, never 0: a print whose end was not measured (a reconciliation or
cleanup close with an estimated end, an operator's close of a stopped print, a terminal
auto-repair), a print whose start was not measured (missing, or reconstructed for a
print adopted mid-flight), a printer without a rate, and a printer record that is gone
all leave both columns empty. The closers without a measured end simply never call
:func:`stamp_measured_end`.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import select

#: Decimals kept on a stored figure. A short print at a small rate is a fraction of a
#: cent; two decimals would round it to nothing before the order's total adds it up.
PRECISION = 3

#: ``extra_data`` key ``services/archive.py`` sets on a print adopted mid-flight whose
#: start was worked out from the printer's remaining time, not observed.
_RECONSTRUCTED_START = "started_at_reconstructed"


def _positive(value) -> float | None:
    """``value`` as a finite number above zero, else None (bool is not a number here)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def wear_for(duration_seconds, rate) -> float | None:
    """Wear of ``duration_seconds`` of printing at ``rate`` money per hour.

    None when the rate is empty or 0 (the feature is off for that printer) or the
    duration is unknown or not positive -- never a zero that would read as "no wear".
    """
    seconds = _positive(duration_seconds)
    per_hour = _positive(rate)
    if seconds is None or per_hour is None:
        return None
    return round(seconds / 3600.0 * per_hour, PRECISION)


def _utc(moment: datetime) -> datetime:
    """Stored timestamps are naive UTC; a fresh ``now`` is aware. Compare them as UTC."""
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def measured_seconds(archive, ended_at) -> float | None:
    """Wall time from the archive's MEASURED start to ``ended_at``; None when either is not measured.

    A start that is missing, or that was reconstructed for a print adopted mid-flight,
    is not a measurement, and neither is an end that does not lie after the start.
    """
    started_at = getattr(archive, "started_at", None)
    if not isinstance(started_at, datetime) or not isinstance(ended_at, datetime):
        return None
    extra = getattr(archive, "extra_data", None)
    if not isinstance(extra, (dict, type(None))):
        return None
    if (extra or {}).get(_RECONSTRUCTED_START):
        return None
    return _positive((_utc(ended_at) - _utc(started_at)).total_seconds())


async def stamp_measured_end(
    db,
    archive,
    *,
    printer_id: int,
    ended_at,
    record_holds: Callable[[], bool],
) -> float | None:
    """Stamp ``archive`` with its wear figure and the rate applied; return the figure written.

    Called by the two terminal writes of a live completion alone, inside the
    transaction that gives the archive its terminal status, with the end that
    transaction writes. ``printer_id`` is the completion's printer; an archive of
    another printer is left alone. The rate is one scalar read on ``db`` (no lock, no
    flush); ``record_holds`` -- the completion's own record-life check -- is asked
    after that read and immediately before the write, with no await in between, so a
    rate read off a printer row that replaced a deleted one (SQLite reuses the id) is
    never applied.

    Never commits and never overwrites a figure already stamped.
    """
    from backend.app.models.printer import Printer

    if archive.wear_cost is not None or archive.wear_rate is not None:
        return None
    if archive.printer_id is None or archive.printer_id != printer_id:
        return None
    seconds = measured_seconds(archive, ended_at)
    if seconds is None:
        return None
    rate = _positive(
        await db.scalar(
            select(Printer.wear_cost_per_hour).where(Printer.id == printer_id).execution_options(autoflush=False)
        )
    )
    figure = wear_for(seconds, rate)
    if figure is None or not record_holds():
        return None
    archive.wear_cost = figure
    archive.wear_rate = rate
    return figure


def carry_restored(archive, wear_cost, wear_rate) -> bool:
    """Write a backup's stored pair onto a restored ``archive``; True when it was written.

    For the Git metadata restore alone, at the place it restores that archive's own
    cost and energy facts and under the same identity checks. The pair is one bundle
    taken as recorded -- nothing is recomputed from a duration or a current rate -- and
    it is refused whole when the figure is not a finite number >= 0 or the rate is not
    a finite number > 0. Never commits and never overwrites a figure already there.
    """
    if archive.wear_cost is not None or archive.wear_rate is not None:
        return False
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (wear_cost, wear_rate)):
        return False
    rate = _positive(wear_rate)
    figure = float(wear_cost)
    if rate is None or not math.isfinite(figure) or figure < 0:
        return False
    archive.wear_cost = round(figure, PRECISION)
    archive.wear_rate = rate
    return True
