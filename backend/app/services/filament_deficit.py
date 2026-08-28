"""Will this print run the spool out, and by how much.

⚠️ **This warns. It never blocks.** A farm routinely finishes a spool mid-plate
and swaps it, and refusing to dispatch would stop work the operator fully
intended. What was missing is being told at all: BamDude's only sufficiency
check lived in the print dialog, so an auto-dispatched job — which never opens
one — went out with nothing said (upstream #2779).

⚠️ **AMS backup slots are pooled into the answer** (upstream `df5aa04d`). With
auto-refill on, the AMS switches to another slot of the same filament when one
runs out, so judging a slot alone reports a shortfall that will never happen —
and a warning that cries wolf is worse than none, because the next one is
ignored too. Backup capacity counts only when the printer says auto-refill is
actually enabled; the setting is per printer and readable from live state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SlotShortfall:
    """One mapped slot that holds less than the print asks of it."""

    slot_label: str
    global_tray_id: int
    needed_grams: float
    available_grams: float

    @property
    def missing_grams(self) -> float:
        return round(max(0.0, self.needed_grams - self.available_grams), 1)


def _slot_label(slot: dict | None, global_tray_id: int) -> str:
    """How the operator refers to this slot — "A1", "Ext", or the raw id.

    The dicts carry ``ams_id`` + ``tray_id`` rather than a printed label, and a
    message that says "tray 5" sends somebody counting.
    """
    if not slot:
        return str(global_tray_id)
    ams_id = slot.get("ams_id")
    tray_id = slot.get("tray_id")
    if not isinstance(ams_id, int) or not isinstance(tray_id, int):
        return str(global_tray_id)
    if ams_id >= 128:
        return "Ext"
    return f"{chr(ord('A') + ams_id)}{tray_id + 1}"


def _same_filament(a: dict, b: dict) -> bool:
    """Whether two loaded slots hold interchangeable filament for refill purposes.

    Type only, deliberately — colour is not part of it. The AMS refills from a
    slot of the same *material*; an operator who loaded two colours of PLA and
    turned auto-refill on has said that is acceptable to them, and second-
    guessing it here would reinstate the false shortfall this pooling exists to
    remove.
    """
    at = (a.get("type") or "").strip().upper()
    bt = (b.get("type") or "").strip().upper()
    return bool(at) and at == bt


def compute_shortfalls(
    requirements: list[dict],
    loaded: list[dict],
    ams_mapping: list[int] | None,
    remaining_by_tray: dict[int, float],
    *,
    auto_refill: bool,
) -> list[SlotShortfall]:
    """Slots the print will exhaust, given what is loaded and how it was mapped.

    ``requirements`` is the 3MF's per-slot demand (``slot_id``, ``used_grams``),
    ``loaded`` the printer's populated slots, ``ams_mapping`` the slicer-slot →
    global-tray array actually dispatched, and ``remaining_by_tray`` grams per
    global tray from whichever source is authoritative for that slot.

    ⚠️ **A tray we have no figure for is skipped, not assumed empty.** Most
    spools are not RFID and most installs do not track every one; treating
    silence as zero would warn on nearly every print and teach the operator to
    ignore the warning.

    ⚠️ **Demand is summed per tray, not per slicer slot.** Two slicer slots can
    map to the same tray, and judged separately each looks satisfied while
    together they empty it.
    """
    if not requirements or not ams_mapping:
        return []

    by_tray: dict[int, float] = {}
    for req in requirements:
        slot_id = req.get("slot_id")
        grams = req.get("used_grams")
        if not isinstance(slot_id, int) or not isinstance(grams, (int, float)) or grams <= 0:
            continue
        if slot_id >= len(ams_mapping):
            continue
        tray = ams_mapping[slot_id]
        if not isinstance(tray, int) or tray < 0:
            continue
        by_tray[tray] = by_tray.get(tray, 0.0) + float(grams)

    # ⚠️ ``global_tray_id`` is the key the scheduler's loaded-slot dicts actually
    # carry (``_build_loaded_filaments``). Naming it anything else here would
    # pass every unit test written against the invented shape and match nothing
    # at runtime.
    loaded_by_tray = {int(s["global_tray_id"]): s for s in loaded if isinstance(s.get("global_tray_id"), int)}

    shortfalls: list[SlotShortfall] = []
    for tray, needed in by_tray.items():
        available = remaining_by_tray.get(tray)
        if available is None:
            continue

        if auto_refill:
            # Every OTHER loaded slot of the same filament the AMS could refill
            # from — and only ones we have a figure for, for the same reason a
            # missing figure is skipped above.
            here = loaded_by_tray.get(tray)
            if here is not None:
                for other_id, other in loaded_by_tray.items():
                    if other_id == tray or other_id in by_tray:
                        continue
                    if _same_filament(here, other) and other_id in remaining_by_tray:
                        available += remaining_by_tray[other_id]

        if available >= needed:
            continue
        label = _slot_label(loaded_by_tray.get(tray), tray)
        shortfalls.append(
            SlotShortfall(
                slot_label=str(label),
                global_tray_id=tray,
                needed_grams=round(needed, 1),
                available_grams=round(available, 1),
            )
        )

    return shortfalls
