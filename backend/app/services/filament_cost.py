"""What a kilogram of filament costs, and what a print therefore costs.

One place, because the answer used to be given in six and two of them
disagreed. An unset rate produced 25.0/kg in the archive service, the archive
routes and the project plan, and 0.0 in ``usage_tracker`` — so on an empty
setting the initial estimate and the untracked-weight top-up costed the same
filament differently, and every one of those numbers was money nobody had
entered.

The farm's rate is whatever the operator stored and nothing otherwise. There is
no sensible default price of plastic: it depends on the material, the brand and
the country, and a plausible-looking figure is worse than a blank, because a
blank is obviously unanswered while 25.00 looks like an answer.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SETTING_KEY = "default_filament_cost"


async def default_rate_per_kg(db) -> float:
    """The farm-wide rate, or 0.0 when the operator has not set one.

    Total: this is read from print-completion paths and background sweeps where
    an exception would surface as a cost that silently stops updating.
    """
    from backend.app.api.routes.settings import get_setting

    try:
        raw = await get_setting(db, SETTING_KEY)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        logger.warning("Could not read %s, treating the rate as unset: %s", SETTING_KEY, exc)
        return 0.0
    if raw in (None, ""):
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("%s is not a number (%r) — treating the rate as unset", SETTING_KEY, raw)
        return 0.0
    return value if value > 0 else 0.0


def cost_of(grams: float | None, rate_per_kg: float) -> float | None:
    """What that much filament costs at that rate, or None when unanswerable.

    None, never 0.0. A zero cost is a claim that the print was free; the absence
    of a rate is a claim about nothing. ``usage_tracker`` has always guarded its
    own writes this way and the other paths did not.
    """
    if not grams or rate_per_kg <= 0:
        return None
    return round((grams / 1000.0) * rate_per_kg, 2)
