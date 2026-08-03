"""Thresholds on sensor readings: the rule, and the two sweeps that apply it.

The rule is a pure function so it can be read in one screen and tested without
a database. Everything below it exists to feed it the newest reading and to
write down what it decided.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

OK = "ok"
ABOVE = "above"
BELOW = "below"


def next_state(
    current: str,
    value: float,
    *,
    min_value: float | None,
    max_value: float | None,
    deadband: float,
) -> str:
    """What this threshold's state becomes, given a reading.

    The deadband applies **only on the way out**. Applying it on the way in
    would mean a threshold of 30 with a deadband of 1 actually alarms at 31,
    and nothing on any screen would say so.

    A limit that is not set can never be crossed: a threshold carrying only a
    maximum never produces ``below``, whatever the reading.
    """
    if max_value is not None and value > max_value:
        return ABOVE
    if min_value is not None and value < min_value:
        return BELOW

    # Inside both raw limits. Whether an existing alarm clears is the only
    # question left, and it is the only place the deadband is consulted.
    if current == ABOVE:
        if max_value is None or value <= max_value - deadband:
            return OK
        return ABOVE
    if current == BELOW:
        if min_value is None or value >= min_value + deadband:
            return OK
        return BELOW
    return OK


def template_for(previous: str, new: str) -> str | None:
    """Which message a transition is, or None when nothing changed.

    "Above" and "below" are different sentences rather than a variable, because
    the sentence is the translation boundary. There is one all-clear: which
    side it returned from is not news.
    """
    if previous == new:
        return None
    if new == ABOVE:
        return "sensor_above_max"
    if new == BELOW:
        return "sensor_below_min"
    return "sensor_back_in_range"
