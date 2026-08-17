"""How much filament a tray has left, asked once.

Three places convert an AMS reading into grams — the inventory sync, the
Spoolman sync, and the live status handler. They agreed by copy, which is how
they would have drifted the moment one of them learned something the others did
not.

BambuStudio settles the same question in ``DevAmsTray::get_filament_remain_weight``
(``DeviceCore/DevFilaSystem.cpp``), and the shape that matters is this:

    if (remain_g >= 0) { return remain_g > 0 ? remain_g : nullopt; }   // no fallthrough
    weight_int = stoi(weight) * remain / 100;
    return weight_int > 0 ? weight_int : nullopt;

⚠️ **BS never returns 0 from that function**, on either path — it returns
``nullopt``. Both readings are firmware sentinels for "I have nothing for you"
far more often than they are a measurement of an empty spool, so BS declines to
answer rather than assert emptiness. We mirror that exactly, including the part
that surprises: **``remain_g == 0`` does NOT fall through to the percentage.**
BS short-circuits on the presence of the field, not on its usefulness, and a
helper that quietly "improved" on that would answer differently from the client
the protocol belongs to.

⚠️ **This is not a stylistic choice — it is the fix for a real incident.** For
six days this module answered ``0.0`` where BS answers ``nullopt``, and
:func:`grams_used` faithfully turned that into ``label_weight - 0``. One AMS push
arriving two seconds after an MQTT reconnect, reporting ``remain: 0`` for three
slots of an X2D, wrote three 1 kg spools off as fully consumed — silently, with
no usage-history row to explain it, and permanently, because the live sync only
ever increases. An empty spool and a spool nobody measured must not be the same
answer; a caller that conflates them records a full spool as spent.

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

    ``None`` is the only way this function declines, and it declines often:
    absent fields, out-of-range percentages, an unknown spool weight — and, as
    in BS, **any reading that works out to zero**. See the module docstring for
    why zero is a sentinel here rather than a measurement.
    """
    if remain_g is not None and remain_g >= 0:
        # BS short-circuits on the field being present, and answers nullopt when
        # it reads 0. Deliberately no fallthrough to the percentage.
        return float(remain_g) if remain_g > 0 else None

    if remain_percent is None or not 0 <= remain_percent <= 100:
        return None
    if not label_weight or label_weight <= 0:
        return None
    left = label_weight * remain_percent / 100.0
    return left if left > 0 else None


def usable_remain_percent(remain: object) -> int | None:
    """A tray's ``remain`` when it is an answer, else ``None``.

    For the **delta** consumers, which never see ``label_weight`` and only
    subtract one reading from another: the remain%-delta fallbacks in
    ``usage_tracker`` (internal inventory) and ``spoolman_tracking`` (Spoolman).
    Same rule as :func:`grams_remaining`, same reason — zero is a sentinel — but
    expressed as "is this reading usable", because that is the whole of what a
    delta path needs to ask.

    ⚠️ **Zero is refused at BOTH ends of a delta, and that costs something.** A
    spool that genuinely ran out mid-print reads 0, and refusing it means that
    print goes unaccounted on this path. Accepted deliberately: the alternative
    is charging ``start - 0``, i.e. up to a whole reel, on a reading the firmware
    hands out whenever it has nothing to say. An under-count is recoverable from
    the next reading; a phantom kilogram is not. The primary paths (3MF, G-code
    layers) are unaffected and still account for the print — the delta fallback
    only ever covers slots they did not.
    """
    if isinstance(remain, bool) or not isinstance(remain, int):
        return None
    if not 0 < remain <= 100:
        return None
    return remain


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
