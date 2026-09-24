"""Resolve an AMS slot's K value from the printer's calibration table.

H2-series trays carry no ``k`` field of their own — only ``cali_idx`` — so the K
value on the AMS slot card is looked up in ``state.kprofiles``. That list holds
every nozzle's table at once (``BambuMQTTClient._store_kprofiles`` files the
replies per diameter), and it is not a clean per-nozzle numbering. Both of these
happen (ported from upstream #2854 + #3044):

* Two profiles can share a ``cali_idx`` and differ by extruder — on an H2C one
  spool read 0.018 on the left nozzle and 0.020 on the right. Resolving on
  ``cali_idx`` alone showed the wrong one.
* One profile can be what *both* extruders' slots point at. An X2D's second AMS
  carried exactly the K values of the first — the same entries, tagged with one
  extruder. Demanding an extruder match left every slot on that AMS blank.

The two are told apart by whether the table distinguishes extruders at all:

1. a profile filed under the slot's own extruder wins outright;
2. if the slot's extruder appears nowhere in the table — or the slot's extruder
   is not known — its tagging carries no information about this slot, so match
   on ``cali_idx`` alone, taking the answer only when the candidates agree on
   one K value, with the diameters currently fitted as the tie-break.

Where the slot's extruder DOES hold profiles and none has this index, that is a
real miss: the index means an entry of *that* nozzle's table, and another
nozzle's entry under the same number is a different profile.

⚠️ Divergence from upstream: an AMS the extruder map does not name (it reports
0xE — uninitialised, or behind a Filament Track Switch, whose inlet binding we do
not parse) is treated as UNKNOWN, not as extruder 0. Upstream defaults it to 0,
which on a dual-nozzle machine prints the right hotend's number on a slot that
may feed the left. On a single-nozzle printer the two readings agree.

If neither step singles out one value the answer is ``None``: a blank on the card
is a smaller error than confidently printing the other nozzle's number.
"""

from __future__ import annotations

from collections.abc import Callable


def slot_extruder(state, ams_id: int, tray_id: int) -> int | None:
    """The extruder an AMS slot feeds, or None when the printer does not say.

    None on every printer without an extruder map (single-nozzle) and for an
    AMS the map does not name. The external holder names its side directly:
    tray 0 is Ext-L (extruder 1), tray 1 is Ext-R (extruder 0).
    """
    extruder_map = getattr(state, "ams_extruder_map", None)
    if not extruder_map:
        return None
    if ams_id == 255:
        return 1 - tray_id if tray_id in (0, 1) else None
    mapped = extruder_map.get(str(ams_id))
    try:
        return int(mapped) if mapped is not None else None
    except (TypeError, ValueError):
        return None


def build_slot_k_resolver(state) -> Callable[[int | None, int, int], float | None]:
    """Return ``resolve(cali_idx, ams_id, tray_id) -> k value or None``.

    Built once per serialization pass and closed over the state, so the REST
    and WebSocket views of the same card cannot answer differently. For the
    external holder pass ``ams_id=255`` and the 0/1 tray index (``vt id - 254``).
    """
    # (extruder, cali_idx) -> {nozzle_diameter: k}. More than one inner entry
    # means two nozzles' tables both claim this index on this extruder.
    table: dict[tuple[int, int], dict[str, float]] = {}
    # cali_idx -> [(nozzle_diameter, k)], every extruder together.
    shared: dict[int, list[tuple[str, float]]] = {}
    for kp in getattr(state, "kprofiles", None) or []:
        if kp.slot_id is None or not kp.k_value:
            continue
        try:
            k_value = float(kp.k_value)
            cali_idx = int(kp.slot_id)
        except (TypeError, ValueError):
            continue  # Skip entries with unparseable values
        try:
            extruder = int(kp.extruder_id or 0)
        except (TypeError, ValueError):
            extruder = 0
        nozzle = _diameter(kp.nozzle_diameter)
        table.setdefault((extruder, cali_idx), {})[nozzle] = k_value
        shared.setdefault(cali_idx, []).append((nozzle, k_value))

    extruders_filed = {extruder for extruder, _ in table}
    installed = {_diameter(n.nozzle_diameter) for n in (getattr(state, "nozzles", None) or []) if n.nozzle_diameter}

    def _agreed(candidates: list[tuple[str, float]]) -> float | None:
        """The one K these candidates describe, or None if they disagree.

        Values rather than entries: two nozzles listing the same number is not
        an ambiguity, it is the shared profile the fallback exists for.
        """
        values = {k for _, k in candidates}
        if len(values) == 1:
            return values.pop()
        live = {k for nozzle, k in candidates if nozzle in installed}
        return live.pop() if len(live) == 1 else None

    def resolve(cali_idx: int | None, ams_id: int, tray_id: int) -> float | None:
        try:
            idx = int(cali_idx) if cali_idx is not None else None
        except (TypeError, ValueError):
            return None
        if idx is None:
            return None
        extruder = slot_extruder(state, ams_id, tray_id)
        if extruder is not None:
            by_nozzle = table.get((extruder, idx))
            if by_nozzle:
                return _agreed(list(by_nozzle.items()))
            if extruder in extruders_filed:
                return None
        candidates = shared.get(idx)
        return _agreed(candidates) if candidates else None

    return resolve


def _diameter(value) -> str:
    """One spelling per diameter, so ``"0.4"`` from the table and ``0.4`` from a
    nozzle report compare equal."""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value or "")
