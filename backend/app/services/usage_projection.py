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
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_INACTIVE: dict = {"active": False}
_SUMMARY_INTERVAL_SECONDS = 300.0


@dataclass
class _ProjectionDiagnostics:
    """Bounded in-process request accounting; access logs retain the detail."""

    started_at: float
    summary_started_at: float
    last_summary_at: float
    requests: int = 0
    inactive: int = 0
    waiting_source: int = 0
    waiting_analysis: int = 0
    ready: int = 0


_projection_diagnostics = _ProjectionDiagnostics(
    started_at=time.monotonic(),
    summary_started_at=time.monotonic(),
    last_summary_at=time.monotonic(),
)


def _record_projection(outcome: str) -> None:
    """Count an endpoint result and periodically emit one useful summary line."""
    diagnostics = _projection_diagnostics
    diagnostics.requests += 1
    setattr(diagnostics, outcome, getattr(diagnostics, outcome) + 1)
    now = time.monotonic()
    if now - diagnostics.last_summary_at < _SUMMARY_INTERVAL_SECONDS:
        return
    window_seconds = round(now - diagnostics.summary_started_at, 1)
    logger.info(
        "[USAGE PROJECTION] summary window_seconds=%s requests=%s inactive=%s waiting_source=%s "
        "waiting_analysis=%s ready=%s",
        window_seconds,
        diagnostics.requests,
        diagnostics.inactive,
        diagnostics.waiting_source,
        diagnostics.waiting_analysis,
        diagnostics.ready,
    )
    diagnostics.summary_started_at = now
    diagnostics.last_summary_at = now
    diagnostics.requests = 0
    diagnostics.inactive = 0
    diagnostics.waiting_source = 0
    diagnostics.waiting_analysis = 0
    diagnostics.ready = 0


def get_usage_projection_diagnostics() -> dict:
    """Return the current low-volume projection counter window for support."""
    diagnostics = _projection_diagnostics
    return {
        "window_seconds": round(time.monotonic() - diagnostics.summary_started_at, 1),
        "requests": diagnostics.requests,
        "inactive": diagnostics.inactive,
        "waiting_source": diagnostics.waiting_source,
        "waiting_analysis": diagnostics.waiting_analysis,
        "ready": diagnostics.ready,
    }


def _slot_consumed_grams(
    slot_id: int,
    estimate_g: float,
    current_layer: int,
    total_layers: int,
    analysis,
) -> float:
    """Consumed-so-far for one slot, capped at the slicer estimate."""
    fraction = analysis.progress_fraction(slot_id - 1, current_layer)
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
    from backend.app.services.print_file_analysis import get_print_file_analysis
    from backend.app.services.print_usage_journal import active_archive_id, load_events
    from backend.app.services.usage_tracker import _decode_mqtt_mapping, journal_boundaries_for_tray

    if printer_manager is None:
        from backend.app.services.printer_manager import printer_manager as _pm

        printer_manager = _pm

    state = printer_manager.get_status(printer_id)
    if state is None or (getattr(state, "state", "") or "").upper() not in ("RUNNING", "PAUSE"):
        _record_projection("inactive")
        return _INACTIVE

    archive_id = await active_archive_id(db, printer_id)
    if archive_id is None:
        _record_projection("inactive")
        return _INACTIVE
    archive = (await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))).scalar_one_or_none()
    if archive is None or not archive.file_path:
        # No 3MF yet (external print mid-download) — nothing to project from.
        _record_projection("waiting_source")
        return {
            "active": True,
            "archive_id": archive_id,
            "print_name": archive.print_name if archive else None,
            "layer_num": getattr(state, "layer_num", 0) or 0,
            "total_layers": getattr(state, "total_layers", 0) or 0,
            "slots": [],
        }

    file_path = app_settings.base_dir / archive.file_path
    current_layer = getattr(state, "layer_num", 0) or 0
    total_layers = getattr(state, "total_layers", 0) or 0

    # The worker can spend seconds on a large archive. The read-only archive
    # lookup above is complete, so return this request's DB connection before
    # waiting; journal and mapping reads below open a short fresh transaction.
    await db.commit()

    # The card may be open in many browsers.  They all await one child-process
    # analysis, but a display poll must not wait long enough to make the UI
    # feel stuck.  The next regular poll sees the completed shared result.
    analysis = await get_print_file_analysis(
        printer_manager,
        printer_id,
        archive_id,
        file_path,
        archive.plate_index,
        timeout=1.0,
    )
    # The child may have taken seconds.  Do not render a finished or replaced
    # run from the archive/state snapshot taken before that wait; the next poll
    # will obtain the new run's ordinary response.
    current_state = printer_manager.get_status(printer_id)
    if (
        current_state is None
        or (getattr(current_state, "state", "") or "").upper() not in ("RUNNING", "PAUSE")
        or await active_archive_id(db, printer_id) != archive_id
    ):
        _record_projection("inactive")
        return _INACTIVE
    if analysis is None:
        _record_projection("waiting_analysis")
        return {
            "active": True,
            "archive_id": archive_id,
            "print_name": archive.print_name,
            "layer_num": current_layer,
            "total_layers": total_layers,
            "slots": [],
        }
    filament_usage = analysis.filament_usage

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
        consumed = _slot_consumed_grams(slot_id, estimate_g, current_layer, total_layers, analysis)

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
                            _slot_consumed_grams(slot_id, estimate_g, end_layer, total_layers, analysis)
                            - _slot_consumed_grams(
                                slot_id, estimate_g, min(start_layer, current_layer), total_layers, analysis
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

    _record_projection("ready")
    return {
        "active": True,
        "archive_id": archive_id,
        "print_name": archive.print_name,
        "layer_num": current_layer,
        "total_layers": total_layers,
        "slots": slots,
    }
