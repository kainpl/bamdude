"""Eligibility evaluation for auto-queue items.

Given an AutoQueueItem and a set of printers already busy in the
current scheduler tick, find an idle printer that:

1. Matches ``target_model`` (case-insensitive, normalised).
2. Matches ``target_location`` if specified.
3. Has ``auto_distribute_eligible=True`` on its PrinterQueue.
4. Is connected (MQTT) and idle (state=IDLE or FINISH/FAILED with
   plate-clear gate released, mirroring per-printer scheduler).
5. Has all ``required_filament_types`` loaded across AMS + external
   trays (canonical-type matching, so PA-CF / PA12-CF / PAHT-CF are
   equivalent — same as upstream).
6. Satisfies ``filament_overrides``: when an override has
   ``force_color_match=True``, the printer must have an exact type+color
   match in some loaded slot. Without the flag, color matches are
   counted as a preference and the highest-scoring printer wins.

This is a near-faithful port of upstream
``PrintScheduler._find_idle_printer_for_model``, adapted to BamDude:
PrinterQueue carries the ``auto_distribute_eligible`` opt-out flag, and
we look up idle state via the existing per-printer scheduler helper.

Returns a tuple ``(printer, waiting_reason)``:
- ``(Printer, None)`` if eligible
- ``(None, reason_string)`` describing why no printer is available
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.auto_queue_ams import _normalize_color_for_compare
from backend.app.services.print_scheduler import _canonical_filament_type, scheduler
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.printer_models import normalize_printer_model

logger = logging.getLogger(__name__)


def _get_missing_filament_types(printer_id: int, required_types: list[str]) -> list[str]:
    """Return the subset of ``required_types`` not loaded on the printer.

    Empty list means all required types are present. Uses canonical-type
    matching for equivalence groups.
    """
    status = printer_manager.get_status(printer_id)
    if not status:
        # Cannot determine; treat as "all missing" (caller skips the printer)
        return list(required_types)

    loaded: set[str] = set()
    for ams_unit in status.raw_data.get("ams", []) or []:
        for tray in ams_unit.get("tray", []) or []:
            t = tray.get("tray_type")
            if t:
                loaded.add(_canonical_filament_type(t))
    for vt in status.raw_data.get("vt_tray") or []:
        t = vt.get("tray_type")
        if t:
            loaded.add(_canonical_filament_type(t))

    return [t for t in required_types if _canonical_filament_type(t) not in loaded]


def _get_missing_force_color_slots(printer_id: int, force_overrides: list[dict]) -> list[str]:
    """For force_color_match overrides, return descriptive strings of unmatched slots.

    Each override must have ``type`` and ``color``. Returns ``"TYPE (color)"``
    for entries that don't have an exact type+color match on the printer.
    """
    status = printer_manager.get_status(printer_id)
    if not status:
        return [f"{o.get('type', '?')} ({o.get('color_name') or o.get('color', '?')})" for o in force_overrides]

    loaded: set[tuple[str, str]] = set()
    for ams_unit in status.raw_data.get("ams", []) or []:
        for tray in ams_unit.get("tray", []) or []:
            t = tray.get("tray_type")
            if t:
                loaded.add((_canonical_filament_type(t), _normalize_color_for_compare(tray.get("tray_color", ""))))
    for vt in status.raw_data.get("vt_tray") or []:
        t = vt.get("tray_type")
        if t:
            loaded.add((_canonical_filament_type(t), _normalize_color_for_compare(vt.get("tray_color", ""))))

    missing: list[str] = []
    for o in force_overrides:
        o_type = _canonical_filament_type(o.get("type") or "")
        o_color = _normalize_color_for_compare(o.get("color") or "")
        if (o_type, o_color) not in loaded:
            color_label = o.get("color_name") or o.get("color", "?")
            missing.append(f"{o_type} ({color_label})")
    return missing


def _count_override_color_matches(printer_id: int, overrides: list[dict]) -> int:
    """Count overrides that have an exact type+color match on the printer.

    Used to rank printers when overrides are preferences, not hard requirements.
    """
    status = printer_manager.get_status(printer_id)
    if not status:
        return 0

    loaded: set[tuple[str, str]] = set()
    for ams_unit in status.raw_data.get("ams", []) or []:
        for tray in ams_unit.get("tray", []) or []:
            t = tray.get("tray_type")
            if t:
                loaded.add((t.upper(), _normalize_color_for_compare(tray.get("tray_color", ""))))
    for vt in status.raw_data.get("vt_tray") or []:
        t = vt.get("tray_type")
        if t:
            loaded.add((t.upper(), _normalize_color_for_compare(vt.get("tray_color", ""))))

    matches = 0
    for o in overrides:
        o_type = (o.get("type") or "").upper()
        o_color = _normalize_color_for_compare(o.get("color") or "")
        if (o_type, o_color) in loaded:
            matches += 1
    return matches


async def find_eligible_printer(
    db: AsyncSession,
    item: AutoQueueItem,
    busy_printers: set[int],
    require_plate_clear: bool = True,
) -> tuple[Printer | None, str | None]:
    """Find an idle printer that satisfies the auto-queue item's requirements.

    The waiting_reason is a user-facing string describing why no printer
    matched (e.g., ``"Waiting for filament: A1mini-01 (needs PETG); A1mini-02 (needs PLA)"``,
    or ``"Busy: A1mini-01, A1mini-02"``). Returns ``(None, None)`` only
    when ``target_model`` is missing.
    """
    if not item.target_model:
        return None, None

    normalized_model = normalize_printer_model(item.target_model) or item.target_model

    # Filter active printers of the right model + location, with auto-distribute eligible.
    query = (
        select(Printer)
        .join(PrinterQueue, PrinterQueue.printer_id == Printer.id)
        .where(func.lower(Printer.model) == normalized_model.lower())
        .where(Printer.is_active.is_(True))
        .where(PrinterQueue.auto_distribute_eligible.is_(True))
    )
    if item.target_location:
        query = query.where(Printer.location == item.target_location)

    result = await db.execute(query)
    printers = list(result.scalars().all())

    location_suffix = f" in {item.target_location}" if item.target_location else ""
    if not printers:
        return None, f"No active {normalized_model} printers{location_suffix} eligible"

    required_types = []
    if item.required_filament_types:
        try:
            import json as _json

            parsed = _json.loads(item.required_filament_types)
            if isinstance(parsed, list):
                required_types = [str(t) for t in parsed if t]
        except (ValueError, TypeError):
            logger.warning("Auto item %s: invalid required_filament_types JSON", item.id)

    filament_overrides: list[dict] = []
    if item.filament_overrides:
        try:
            import json as _json

            parsed = _json.loads(item.filament_overrides)
            if isinstance(parsed, list):
                filament_overrides = [o for o in parsed if isinstance(o, dict)]
        except (ValueError, TypeError):
            logger.warning("Auto item %s: invalid filament_overrides JSON", item.id)

    force_overrides = [o for o in filament_overrides if o.get("force_color_match")]
    pref_overrides = [o for o in filament_overrides if not o.get("force_color_match")]

    printers_busy: list[str] = []
    printers_offline: list[str] = []
    printers_missing_filament: list[tuple[str, list[str]]] = []
    candidates: list[tuple[Printer, int]] = []  # (printer, color_match_count)

    for printer in printers:
        if printer.id in busy_printers:
            # Already claimed in this tick. Surface "missing color" if force-color
            # would have failed anyway, so the user knows it needs a filament change.
            if force_overrides and not pref_overrides:
                missing_colors = _get_missing_force_color_slots(printer.id, force_overrides)
                if missing_colors:
                    printers_missing_filament.append((printer.name, missing_colors))
                    continue
            printers_busy.append(printer.name)
            continue

        is_connected = printer_manager.is_connected(printer.id)
        if not is_connected:
            printers_offline.append(printer.name)
            continue

        is_idle = scheduler._is_printer_idle(printer.id, require_plate_clear)
        if not is_idle:
            if force_overrides and not pref_overrides:
                missing_colors = _get_missing_force_color_slots(printer.id, force_overrides)
                if missing_colors:
                    printers_missing_filament.append((printer.name, missing_colors))
                    continue
            printers_busy.append(printer.name)
            continue

        if required_types:
            missing = _get_missing_filament_types(printer.id, required_types)
            if missing:
                if force_overrides:
                    force_color_map = {
                        (o.get("type") or "").upper(): o.get("color_name") or o.get("color", "?")
                        for o in force_overrides
                    }
                    missing = [
                        f"{t} ({force_color_map[t_upper]})" if (t_upper := t.upper()) in force_color_map else t
                        for t in missing
                    ]
                printers_missing_filament.append((printer.name, missing))
                continue

        if force_overrides:
            missing_colors = _get_missing_force_color_slots(printer.id, force_overrides)
            if missing_colors:
                printers_missing_filament.append((printer.name, missing_colors))
                continue

        if pref_overrides:
            color_matches = _count_override_color_matches(printer.id, pref_overrides)
            if color_matches > 0:
                candidates.append((printer, color_matches))
            else:
                pref_descriptions = [f"{o.get('type', '?')} ({o.get('color', '?')})" for o in pref_overrides]
                printers_missing_filament.append((printer.name, pref_descriptions))
                continue
        elif force_overrides:
            return printer, None
        else:
            return printer, None

    if candidates:
        candidates.sort(key=lambda c: c[1], reverse=True)
        return candidates[0][0], None

    reasons: list[str] = []
    if printers_missing_filament:
        if force_overrides and not pref_overrides and not printers_busy:
            all_missing = sorted({c for _, cols in printers_missing_filament for c in cols})
            return None, f"No matching material/color. Waiting on {', '.join(all_missing)}"
        names_and_missing = [f"{name} (needs {', '.join(miss)})" for name, miss in printers_missing_filament]
        reasons.append(f"Waiting for filament: {'; '.join(names_and_missing)}")
    if printers_busy:
        reasons.append(f"Busy: {', '.join(printers_busy)}")
    if printers_offline:
        reasons.append(f"Offline: {', '.join(printers_offline)}")

    return None, " | ".join(reasons) if reasons else f"No available {normalized_model} printers{location_suffix}"
