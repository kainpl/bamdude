"""How much filament a tray has left, asked once.

Three places convert an AMS reading into grams used — the inventory sync, the
Spoolman sync, and the live status handler. They agreed by copy, which is how
they would have drifted the moment one of them learned something the others did
not.

BambuStudio settles the same question in ``DevAmsTray::get_filament_remain_weight``
with a clear precedence:

    if remain_g >= 0:  the firmware's own grams win (0 means "nothing left")
    else:              spool weight x remain% / 100

⚠️ **``remain_g`` is not a weight measurement.** No Bambu AMS weighs anything —
the 2 Pro included. The figure comes from the RFID tag plus how far the spool
has turned, so it exists for tagged spools and is ``-1`` everywhere else; a load
cell is an open feature request, not a product. Reading it is therefore not
about better hardware but about using the better number when it is offered.

The percentage path stays, because it is what almost every tray actually
provides. ⚠️ It is coarse in a way worth remembering: ``remain`` is an integer
percent, so one step is 10 g on a 1 kg spool, and that is the floor on how
precise any of this can be.
"""

from __future__ import annotations

# What BambuStudio uses for "the printer did not tell us".
REMAIN_G_ABSENT = -1


def grams_remaining(remain_g: int | None, remain_percent: int | None, label_weight: int | None) -> float | None:
    """Grams left on a spool, or ``None`` when nothing usable was reported.

    ``None`` is deliberately distinct from ``0.0``: an empty spool and a spool we
    know nothing about are different answers, and a caller that treats them
    alike will happily record a full spool as spent.
    """
    if remain_g is not None and remain_g >= 0:
        return float(remain_g)

    if remain_percent is None or not 0 <= remain_percent <= 100:
        return None
    if not label_weight or label_weight <= 0:
        return None
    return label_weight * remain_percent / 100.0


def grams_used(remain_g: int | None, remain_percent: int | None, label_weight: int | None) -> float | None:
    """The mirror of :func:`grams_remaining` — how much has gone.

    ⚠️ Needs ``label_weight`` even on the ``remain_g`` path: firmware reports
    what is LEFT, and "used" only exists relative to what the spool started
    with. A spool whose advertised weight is unknown has a knowable remainder
    and an unknowable consumption, and saying otherwise would invent a number.
    """
    left = grams_remaining(remain_g, remain_percent, label_weight)
    if left is None or not label_weight or label_weight <= 0:
        return None
    return round(max(0.0, label_weight - left), 1)
