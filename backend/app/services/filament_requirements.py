"""Parse per-slot filament requirements out of a 3MF file.

The auto-queue intake used to pull only `required_filament_types` (a
de-duplicated list) out of a 3MF and drop the per-slot ``color`` info on the
floor. That meant the eligibility check had no way to express "slot 1 needs
red PLA, slot 2 needs green PLA" — the scheduler matched on canonical type
only and dispatched onto whichever printer happened to be free, even when
its loaded slots had the right material in the wrong colours.

This helper is the shared per-slot extractor: it returns one dict per slot
that actually consumed filament on the chosen plate. Callers wire the list
into ``filament_overrides`` (with ``force_color_match=True`` when they want
the scheduler to refuse colour mismatches) so the existing
:func:`backend.app.services.auto_queue_eligibility._get_missing_force_color_slots`
path can do exact type+colour matching against printer AMS state.

Returned shape mirrors the override JSON the eligibility helper validates
against:

    [{"slot_id": int, "type": str, "color": str, "used_grams": float,
      "nozzle_id": int | None}, ...]

— minus the ``force_color_match`` flag, which the caller adds based on
its own setting (the per-VP ``queue_force_color_match`` toggle, in our
case).

Defensive: malformed / missing 3MF returns ``[]`` so callers treat it as
"no requirements detected" rather than an exception, which keeps the VP
upload path forgiving — the slicer already produced something printable,
worst case the queue item just lacks the auto-extracted overrides.
"""

from __future__ import annotations

import logging
from pathlib import Path

from backend.app.services.archive import ThreeMFParser

logger = logging.getLogger(__name__)


def extract_filament_requirements(file_path: Path | str, plate_id: int | None = None) -> list[dict]:
    """Return ``[{slot_id, type, color, used_grams, [nozzle_id]}]`` from a 3MF.

    Args:
        file_path: Path to the 3MF on disk.
        plate_id: 1-indexed plate to extract for. ``None`` falls through to
            ThreeMFParser's default plate (plate 1 for single-plate exports).

    Returns:
        List of per-slot filament dicts, sorted by ``slot_id``. Empty list
        when the 3MF is unreadable, has no slice_info, or no filaments
        consumed any material on the chosen plate.
    """
    path = Path(file_path)
    if not path.exists():
        return []

    try:
        parser = ThreeMFParser(path, plate_number=plate_id)
        md = parser.parse()
    except Exception as e:  # noqa: BLE001 — defensive: never raise from intake path
        logger.warning("Failed to parse filament requirements from %s: %s", path, e)
        return []

    raw_slots = md.get("filament_slots") or []
    out: list[dict] = []
    for slot in raw_slots:
        slot_id = slot.get("slot_id")
        ftype = slot.get("type")
        used_g = slot.get("used_g") or 0
        if slot_id is None or not ftype:
            continue
        try:
            used_grams = float(used_g)
        except (TypeError, ValueError):
            continue
        if used_grams <= 0:
            # Slot present in the slicer config but not consumed by this plate
            # — irrelevant to routing, skip so the override list doesn't carry
            # phantom requirements.
            continue
        entry: dict = {
            "slot_id": int(slot_id),
            "type": ftype,
            "color": slot.get("color") or "",
            "used_grams": used_grams,
        }
        # ThreeMFParser's per-plate metadata already merges in the
        # gcode-derived nozzle mapping when the file is a sliced
        # ``.gcode.3mf``; surface it for callers that route by extruder.
        nozzle_id = slot.get("nozzle_id")
        if nozzle_id is not None:
            try:
                entry["nozzle_id"] = int(nozzle_id)
            except (TypeError, ValueError):
                pass
        out.append(entry)

    out.sort(key=lambda x: x["slot_id"])
    return out
