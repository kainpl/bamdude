"""Re-pricing uses the rate rule the usage tracker books a row with (owner's Р4).

``filament_cost.usage_cost`` is what ``usage_reprice`` writes into a recorded
row. The tracker's own bookings keep their inline form — the accounting module
is not refactored for a correction door — so this test holds the two together:
the same figures for the same inputs, and a failure the day the tracker's form
changes, pointing here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app.services.filament_cost import usage_cost

TRACKER = Path(__file__).resolve().parents[2] / "app" / "services" / "usage_tracker.py"

RATE = "cost_per_kg = spool.cost_per_kg if spool.cost_per_kg is not None else default_filament_cost"
COST = re.compile(r"cost = round\(\((\w+) / 1000\.0\) \* cost_per_kg, 2\)")


def _tracker(grams: float, spool_price: float | None, default_rate: float) -> float | None:
    """The tracker's booking, as written at each of its sites."""
    cost = None
    cost_per_kg = spool_price if spool_price is not None else default_rate
    if cost_per_kg > 0:
        cost = round((grams / 1000.0) * cost_per_kg, 2)
    return cost


@pytest.mark.parametrize(
    ("grams", "spool_price", "default_rate"),
    [
        (100.0, 20.0, 25.0),  # the spool's own price wins
        (100.0, 200.0, 25.0),
        (87.3, 31.99, 0.0),
        (100.0, None, 25.0),  # no price on the spool: the farm's rate
        (100.0, None, 0.0),  # no price anywhere: no cost, never 0.00
        (100.0, 0.0, 25.0),  # a spool priced 0 is free, not "use the default"
        (0.37, 18.0, 25.0),
        (1250.5, 26.0, 25.0),
    ],
)
def test_the_same_figure_as_the_trackers_booking(grams, spool_price, default_rate):
    assert usage_cost(grams, spool_price, default_rate) == _tracker(grams, spool_price, default_rate)


def test_the_tracker_still_books_with_that_form():
    """If this fails the tracker's rate rule moved: bring ``filament_cost.usage_cost`` with it."""
    source = TRACKER.read_text(encoding="utf-8")

    rates = source.count(RATE)
    costs = COST.findall(source)

    assert rates >= 1 and rates == len(costs), (rates, costs)
