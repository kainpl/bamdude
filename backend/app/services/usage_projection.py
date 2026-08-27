"""Live filament-usage projection for an active print. Display-only.

Answers "how much has this print eaten so far, and who is being charged for
the current segment" from the same sources the completion accountant uses —
per-filament G-code cumulative at the current layer (linear estimate-by-layers
fallback) and the usage journal's frozen spool boundaries — but writes
NOTHING. The books are written once, at completion; a projection that
persisted anything would need reconciling against the final rows, which is
the class of double-count the whole tracker is built to avoid.
"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_INACTIVE: dict = {"active": False}


def _slot_consumed_grams(
    slot_id: int,
    estimate_g: float,
    current_layer: int,
    total_layers: int,
    layer_usage: dict | None,
    props: dict,
) -> float:
    """Consumed-so-far for one slot, capped at the slicer estimate."""
    from backend.app.utils import threemf_tools

    fraction = threemf_tools.slot_progress_fraction(layer_usage, slot_id - 1, current_layer)
    if fraction is not None:
        # Progress fraction x slicer estimate, NOT absolute gcode grams: the
        # flush on every filament change lives in firmware macros and never
        # appears as gcode extrusion, so the absolute figure showed 3 g at
        # layer 11 of a swap-heavy print whose real consumption was ~7x that.
        grams = estimate_g * fraction
    elif total_layers > 0:
        grams = estimate_g * min(current_layer / total_layers, 1.0)
    else:
        grams = 0.0
    return round(min(grams, estimate_g), 1)


async def compute_usage_projection(db: AsyncSession, printer_id: int, printer_manager=None) -> dict:
    """The projection payload, or ``{"active": False}`` when nothing runs."""
    from backend.app.core.config import settings as app_settings
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.print_usage_journal import active_archive_id, load_events
    from backend.app.services.usage_tracker import _decode_mqtt_mapping, journal_boundaries_for_tray
    from backend.app.utils import threemf_tools

    if printer_manager is None:
        from backend.app.services.printer_manager import printer_manager as _pm

        printer_manager = _pm

    state = printer_manager.get_status(printer_id)
    if state is None or (getattr(state, "state", "") or "").upper() not in ("RUNNING", "PAUSE"):
        return _INACTIVE

    archive_id = await active_archive_id(db, printer_id)
    if archive_id is None:
        return _INACTIVE
    archive = (await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))).scalar_one_or_none()
    if archive is None or not archive.file_path:
        # No 3MF yet (external print mid-download) — nothing to project from.
        return {
            "active": True,
            "archive_id": archive_id,
            "print_name": archive.print_name if archive else None,
            "layer_num": getattr(state, "layer_num", 0) or 0,
            "total_layers": getattr(state, "total_layers", 0) or 0,
            "slots": [],
        }

    file_path = app_settings.base_dir / archive.file_path
    if not file_path.exists():
        return {
            "active": True,
            "archive_id": archive_id,
            "print_name": archive.print_name,
            "layer_num": getattr(state, "layer_num", 0) or 0,
            "total_layers": getattr(state, "total_layers", 0) or 0,
            "slots": [],
        }

    filament_usage = threemf_tools.extract_filament_usage_from_3mf(file_path, archive.plate_index)
    layer_usage = threemf_tools.extract_layer_filament_usage_from_3mf(file_path, archive.plate_index)
    filament_props = threemf_tools.extract_filament_properties_from_3mf(file_path)

    current_layer = getattr(state, "layer_num", 0) or 0
    total_layers = getattr(state, "total_layers", 0) or 0

    events = await load_events(db, printer_id, archive_id)

    # Slot → tray, the completion path's priority without the in-memory copy:
    # the queue item's dispatched mapping (survives restarts), then the live
    # MQTT field. Only needed to find journal boundaries — a slot with no
    # resolvable tray still projects its total, just without segments.
    slot_to_tray: list | None = None
    queue_item = (
        (
            await db.execute(
                select(PrintQueueItem)
                .where(PrintQueueItem.archive_id == archive_id)
                .where(PrintQueueItem.status.in_(["printing", "completed", "failed"]))
            )
        )
        .scalars()
        .first()
    )
    if queue_item and queue_item.ams_mapping:
        try:
            slot_to_tray = json.loads(queue_item.ams_mapping)
        except (json.JSONDecodeError, TypeError):
            slot_to_tray = None
    if not slot_to_tray:
        raw = getattr(state, "raw_data", None) or {}
        slot_to_tray = _decode_mqtt_mapping(raw.get("mapping"))

    slots = []
    for usage in filament_usage:
        slot_id = usage.get("slot_id", 0)
        estimate_g = float(usage.get("used_g", 0) or 0)
        if slot_id <= 0 or estimate_g <= 0:
            continue
        props = filament_props.get(slot_id, {})
        consumed = _slot_consumed_grams(slot_id, estimate_g, current_layer, total_layers, layer_usage, props)

        slot_payload: dict = {
            "slot_id": slot_id,
            "type": usage.get("type", ""),
            "color": usage.get("color", ""),
            "estimate_g": round(estimate_g, 1),
            "consumed_g": consumed,
        }

        tray = None
        if slot_to_tray and 0 < slot_id <= len(slot_to_tray):
            mapped = slot_to_tray[slot_id - 1]
            if isinstance(mapped, int) and mapped >= 0:
                tray = mapped
        if tray is not None and events:
            boundaries = journal_boundaries_for_tray(events, tray)
            if len(boundaries) > 1:
                segments = []
                for idx, (start_layer, spool_id, spoolman_spool_id) in enumerate(boundaries):
                    end_layer = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else current_layer
                    end_layer = min(end_layer, current_layer)
                    if end_layer <= start_layer and idx > 0:
                        seg_consumed = 0.0
                    else:
                        seg_consumed = round(
                            _slot_consumed_grams(slot_id, estimate_g, end_layer, total_layers, layer_usage, props)
                            - _slot_consumed_grams(
                                slot_id, estimate_g, min(start_layer, current_layer), total_layers, layer_usage, props
                            ),
                            1,
                        )
                    segments.append(
                        {
                            "start_layer": start_layer,
                            "spool_id": spool_id,
                            "spoolman_spool_id": spoolman_spool_id,
                            "consumed_g": max(seg_consumed, 0.0),
                        }
                    )
                # Segments are a DISPLAY of attribution — a runout the user
                # resumed without replacing keeps both segments on the same
                # reel, and showing that as "split across spools" is noise
                # (measured complaint, 2026-08-23). Emit only a real split.
                distinct = {(seg["spool_id"], seg["spoolman_spool_id"]) for seg in segments}
                if len(distinct) > 1:
                    slot_payload["segments"] = segments
        slots.append(slot_payload)

    return {
        "active": True,
        "archive_id": archive_id,
        "print_name": archive.print_name,
        "layer_num": current_layer,
        "total_layers": total_layers,
        "slots": slots,
    }
