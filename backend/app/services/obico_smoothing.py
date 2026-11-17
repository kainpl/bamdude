"""Temporal smoothing for Obico ML detection scores.

A port of Obico's own decision -- obico-server ``backend/lib/prediction.py``,
``update_prediction_with_detections`` and ``is_failing``, with the parameters of
``FD_1ST_GEN_PARAMS``. The arithmetic is copied, quirks included, so a verdict here
is the verdict Obico's own server would reach on the same frames:

- per-frame ``current_p`` = sum of detection confidences
- ``ewm_mean`` = exponentially weighted mean (alpha = 2 / (span + 1), span = 12),
  starting from 0 at each print
- ``rolling_mean_short`` = Obico's streaming approximation of the mean of the
  print's last 310 frames
- ``rolling_mean_long`` = the same approximation over the PRINTER's last 7200
  frames (~20 h at 10 s), carried across its prints: whatever its camera always
  sees -- plate texture, a label, the purge chute -- is baseline, not failure
- the score is ``ewm_mean - rolling_mean_long``. Below LOW it is safe, above HIGH
  failing; in between it is failing only when it also beats
  ``(rolling_mean_short - rolling_mean_long) * 3.8`` -- a detection that appeared,
  not one that crept up
- "warning" is failing at the plain thresholds (Obico's alert); "failure" is
  failing with the score divided by 1.75 (Obico's pause)
- the first frames of a print are always safe

Obico multiplies the score by the printer's sensitivity; scaling LOW / HIGH by its
inverse is the same decision, and the scaled thresholds are what the status panel
shows.

Until 2026-11 this module returned ``max(ewm_mean, short - long)`` and compared it
with LOW / HIGH: the baseline step was missing, so a background the model always
scores added straight into the result and ended in "failure" (upstream #3459).
"""

import math
from dataclasses import dataclass, field

EWM_SPAN = 12
EWM_ALPHA = 2.0 / (EWM_SPAN + 1)
ROLLING_SHORT = 310
ROLLING_LONG = 7200
# Obico's INIT_SAFE_FRAME_NUM: a print's frames numbered below it are safe.
WARMUP_FRAMES = 30
ROLLING_MEAN_SHORT_MULTIPLE = 3.8
ESCALATING_FACTOR = 1.75

# Base thresholds; sensitivity multipliers adjust them
BASE_LOW = 0.38
BASE_HIGH = 0.78

SENSITIVITY_MULT = {
    "low": 1.25,  # harder to trigger — higher thresholds
    "medium": 1.0,
    "high": 0.75,  # easier to trigger — lower thresholds
}


def thresholds(sensitivity: str) -> tuple[float, float]:
    mult = SENSITIVITY_MULT.get(sensitivity, 1.0)
    return BASE_LOW * mult, BASE_HIGH * mult


def _next_rolling_mean(value: float, mean: float, count: int, window: int) -> float:
    """Obico's ``next_rolling_mean``: ``count`` already includes this frame."""
    return mean + (value - mean) / float(window if window <= count else count + 1)


@dataclass
class Baseline:
    """A printer's long-term mean, carried across its prints. Two numbers, in memory."""

    mean: float = 0.0
    frames: int = 0

    def add(self, value: float) -> None:
        self.frames += 1
        self.mean = _next_rolling_mean(value, self.mean, self.frames, ROLLING_LONG)


@dataclass(frozen=True)
class Reading:
    """One frame's smoothed state, as Obico's ``is_failing`` reads it."""

    score: float = 0.0  # ewm_mean - rolling_mean_long
    short_rise: float = 0.0  # rolling_mean_short - rolling_mean_long
    warmup: bool = True


@dataclass
class PrintState:
    """Per-print smoothing state. A new print gets a new one; the baseline it is
    given belongs to the printer and outlives it."""

    baseline: Baseline = field(default_factory=Baseline)
    frame_count: int = 0
    ewm_mean: float = 0.0
    short_mean: float = 0.0
    last: Reading = field(default_factory=Reading)

    def update(self, current_p: float) -> Reading:
        """Feed a new per-frame score and return the smoothed reading."""
        self.frame_count += 1
        self.ewm_mean = EWM_ALPHA * current_p + (1 - EWM_ALPHA) * self.ewm_mean
        self.short_mean = _next_rolling_mean(current_p, self.short_mean, self.frame_count, ROLLING_SHORT)
        self.baseline.add(current_p)
        self.last = Reading(
            score=self.ewm_mean - self.baseline.mean,
            short_rise=self.short_mean - self.baseline.mean,
            warmup=self.frame_count < WARMUP_FRAMES,
        )
        return self.last


def _is_failing(reading: Reading, sensitivity: str, escalating_factor: float) -> bool:
    if reading.warmup:
        return False
    low, high = thresholds(sensitivity)
    score = reading.score / escalating_factor
    if score < low:
        return False
    if score > high:
        return True
    # The thresholds carry the sensitivity here while Obico scales the score, so the
    # short-window check takes the same factor to compare like with like.
    mult = SENSITIVITY_MULT.get(sensitivity, 1.0)
    return score > reading.short_rise * ROLLING_MEAN_SHORT_MULTIPLE * mult


def classify(reading: Reading, sensitivity: str) -> str:
    """'safe', 'warning' (Obico would alert) or 'failure' (Obico would pause)."""
    if _is_failing(reading, sensitivity, ESCALATING_FACTOR):
        return "failure"
    if _is_failing(reading, sensitivity, 1.0):
        return "warning"
    return "safe"


def score_from_detections(detections: list) -> float:
    """Sum confidences from the ML API `detections` array.

    Each detection is `[label, confidence, [x, y, w, h]]`. We only care about
    the confidence column — label is always "failure" for the single-class model.
    """
    total = 0.0
    for det in detections or []:
        try:
            value = float(det[1])
        except (IndexError, TypeError, ValueError):
            continue
        if math.isnan(value) or math.isinf(value):
            continue
        total += value
    return total
