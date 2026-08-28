"""What the chamber has already been through, so the soak need not repeat it.

Back-to-back prints in chamber-heated materials (ASA, ABS, PA, PC) each paid a
full heat-soak from cold, even when the print that just finished had left the
chamber at temperature. This records a rolling window of chamber readings so
:mod:`preheat` can credit time already spent at temperature against the
configured soak.

⚠️ **This module only remembers; it never heats and never cools.** Upstream
paired it with a "keep the bed warm between prints" hold, which BamDude
deliberately does not have — see the vault note on why (a hot bed is what you
wait out to release a part, and the plastics that need a chamber must not cool
outside one anyway).

The three rules the credit obeys, and why:

* **Nothing recent, nothing credited.** A two-hour history whose last reading is
  half an hour old is not evidence of anything: the measured cooling rate is
  fast enough to cross the threshold unobserved in that time.
* **Only the most recent unbroken run counts.** A gap wider than
  :data:`_SAMPLE_MAX_GAP_SECONDS` is a disconnect, and time on the far side of
  it is not evidence of temperature.
* **A dip must last to count.** An enclosed chamber's thermal mass cannot lose
  and regain several degrees quickly, so a brief low reading is a door opening
  or sensor noise, not lost soak.
"""

from __future__ import annotations

import logging
import time
from collections import deque

logger = logging.getLogger(__name__)

# How far back readings are kept. Long enough to cover a plate change plus the
# tail of the previous print; anything older cannot inform the next soak.
_HISTORY_TTL_SECONDS = 2 * 60 * 60

# Largest acceptable gap between two consecutive samples before the older one is
# treated as a separate observation run. The scheduler ticks every 3-30 s, so
# this is that cadence with a wide safety margin.
_SAMPLE_MAX_GAP_SECONDS = 60.0

# How long the chamber must read below target before we accept that it really
# cooled.
#
# ⚠️ The number is thermal, not arbitrary. An enclosed chamber cannot lose and
# regain several degrees quickly: measured on an X1C, falling from ~55 °C to
# below 48 °C took 23-73 minutes (~0.2 °C/min), while the fastest drop ever
# recorded in the same data was 27 °C/min — impossible for that mass, i.e. a
# sensor artifact. A brief sub-target reading is therefore a door opening or
# noise rather than lost soak, and a plate change produces one. Six minutes
# clears the longest such artifact observed (~5 min once bracketed by its
# neighbouring samples) and still sits far below the 23-minute floor for real
# cooling.
_DIP_GRACE_SECONDS = 360.0

# How far below target still counts as "at temperature".
_DEFAULT_TOLERANCE = 2.0

# printer_id -> deque of (monotonic timestamp, celsius). Pruned on write.
_history: dict[int, deque[tuple[float, float]]] = {}


def record(printer_id: int, celsius: float) -> None:
    """Add one reading, dropping anything past the window."""
    now = time.monotonic()
    samples = _history.setdefault(printer_id, deque())
    samples.append((now, float(celsius)))
    cutoff = now - _HISTORY_TTL_SECONDS
    while samples and samples[0][0] < cutoff:
        samples.popleft()


def sample_all(statuses: dict[int, object]) -> None:
    """Record a reading for every connected printer that reports a chamber.

    Called once per scheduler tick. Printers no longer in ``statuses`` are
    dropped, so nothing accumulates for a machine that was deleted.
    """
    for printer_id, status in statuses.items():
        if status is None or not getattr(status, "connected", False):
            continue
        chamber = (getattr(status, "temperatures", None) or {}).get("chamber")
        if chamber is None:
            continue
        try:
            record(printer_id, float(chamber))
        except (TypeError, ValueError):
            continue
    for printer_id in list(_history):
        if printer_id not in statuses:
            _history.pop(printer_id, None)


def forget(printer_id: int) -> None:
    """Drop a printer's history — it says nothing about the chamber any more."""
    _history.pop(printer_id, None)


def soak_remaining(
    printer_id: int,
    chamber_target: float,
    soak_seconds: int,
    tolerance: float = _DEFAULT_TOLERANCE,
) -> int:
    """How many seconds of soak are still owed, after crediting what is known.

    Returns ``soak_seconds`` unchanged when nothing can be credited — no
    history, stale history, or a chamber below the threshold right now — and 0
    once the credited time covers the whole soak.

    ⚠️ Fails towards the full soak in every uncertain case. Crediting a soak
    that did not happen starts a print on a cold chamber, which is a warped
    part; refusing to credit one that did costs some minutes.
    """
    samples = list(_history.get(printer_id) or ())
    if not samples:
        return soak_seconds

    now = time.monotonic()
    newest_ts, newest_temp = samples[-1]
    if now - newest_ts > _SAMPLE_MAX_GAP_SECONDS:
        return soak_seconds  # stale: the chamber may have cooled unobserved

    threshold = chamber_target - tolerance
    if newest_temp < threshold:
        return soak_seconds  # below target right now, so nothing is soaked

    # Earliest point we have unbroken observations for.
    credit_from = samples[-1][0]
    for i in range(len(samples) - 1, 0, -1):
        if samples[i][0] - samples[i - 1][0] > _SAMPLE_MAX_GAP_SECONDS:
            break
        credit_from = samples[i - 1][0]

    # Pull the credit forward past the last dip that lasted. Each excursion is
    # measured between the in-range readings that bracket it, so a lone stray
    # sample is charged one sampling interval rather than zero and the
    # comparison leans towards calling a dip real.
    i = 0
    while i < len(samples):
        if samples[i][1] >= threshold:
            i += 1
            continue
        j = i
        while j < len(samples) and samples[j][1] < threshold:
            j += 1
        # The newest sample is at or above the threshold (checked above), so j
        # is always in range here.
        opened_at = samples[i - 1][0] if i > 0 else samples[i][0]
        if samples[j][0] - opened_at >= _DIP_GRACE_SECONDS:
            # Credit resumes at the last below-threshold sample rather than the
            # first good one after it, so a recovered dip over-credits by up to
            # one sampling interval — the opposite lean to the bracketing above.
            # Both are bounded by the sample cadence and dwarfed by the grace
            # period, so neither is worth the arithmetic to remove.
            credit_from = max(credit_from, samples[j - 1][0])
        i = j

    return max(0, soak_seconds - int(now - credit_from))
