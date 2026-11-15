"""The size and the price of a Spoolman spool, read one way everywhere.

Spoolman keeps a full spool's net weight on the spool (``initial_weight``) and
falls back to its filament's catalogue ``weight``, so one filament can have
spools of different sizes; and a spool's ``price`` is what that spool cost, not
a rate. Reading only the filament made a 250 g spool of a 1000 g filament look
— and cost — four times its size (upstream #3194).

Pure functions over the raw ``GET /spool`` dict, with no dependency of their
own: what the Filaments page shows and what a print is charged both come from
here, so the two cannot drift apart.
"""

from __future__ import annotations

import math


def _positive(value: object) -> float | None:
    """A finite number above zero, or None. A bool is not a number here:
    ``True`` must not read as a 1 g spool or a price of 1."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def spool_net_weight(spool: dict | None) -> float | None:
    """Grams of filament on a full spool: its own ``initial_weight``, else its
    filament's ``weight``; None when neither is a positive number."""
    if not isinstance(spool, dict):
        return None
    own = _positive(spool.get("initial_weight"))
    if own is not None:
        return own
    filament = spool.get("filament")
    return _positive(filament.get("weight")) if isinstance(filament, dict) else None


def spool_price_per_gram(spool: dict | None) -> float | None:
    """What a gram off this spool costs: the spool's own ``price``, else its
    filament's, over the spool's net weight.

    A spool-level 0 or null falls through to the filament (importers write 0
    for "not set"). None — unpriced, never free — when there is no positive
    price or no net weight.
    """
    net = spool_net_weight(spool)
    if net is None:
        return None
    price = _positive(spool.get("price"))
    if price is None:
        filament = spool.get("filament")
        price = _positive(filament.get("price")) if isinstance(filament, dict) else None
    return price / net if price is not None else None
