"""What a Spoolman spool weighs and what a gram of it costs — one rule for every reader.

A Spoolman spool carries its own net weight (``initial_weight``) beside the
catalogue figure of its filament (``filament.weight``). They differ whenever a
spool is not the catalogue size — a 250 g refill registered under a 1000 g
filament — and every figure derived from the weight (a remain percentage turned
into grams, the price of one gram) must then follow the SPOOL, or it is off by
the ratio of the two sizes (upstream 2f7f17a89, #3194).
"""

import math


def _positive(value) -> float | None:
    """``value`` as a float when it is a usable positive quantity, else None.

    Rejects bools (``float(True)`` is 1.0 — a weight of 1 g would price a spool
    per gram at its whole cost), and NaN / infinity, which compare False against
    every bound and would reach an archive as a cost nothing later can clear.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def spool_net_weight(spool: dict | None) -> float | None:
    """The spool's net filament weight in grams, or None when nothing states it.

    The spool's own ``initial_weight`` when positive, else its filament's
    catalogue ``weight`` when positive.
    """
    if not isinstance(spool, dict):
        return None
    own = _positive(spool.get("initial_weight"))
    if own is not None:
        return own
    filament = spool.get("filament")
    if not isinstance(filament, dict):
        return None
    return _positive(filament.get("weight"))


def spool_price_per_gram(spool: dict | None) -> float | None:
    """What one gram off this spool costs, or None when it is unpriced or weightless.

    The spool's own ``price`` when positive, else its filament's catalogue
    ``price`` when positive — either divided by :func:`spool_net_weight`. A
    missing or non-positive price is an UNPRICED spool, not a free one; a
    spool-level 0 falls through to the catalogue (importers write 0 for "not
    set" often enough).
    """
    if not isinstance(spool, dict):
        return None
    filament = spool.get("filament")
    if not isinstance(filament, dict):
        filament = {}
    price = _positive(spool.get("price"))
    if price is None:
        price = _positive(filament.get("price"))
    weight = spool_net_weight(spool)
    if price is None or weight is None:
        return None
    # Two finite operands can still overflow into a non-finite quotient.
    rate = price / weight
    return rate if math.isfinite(rate) else None
