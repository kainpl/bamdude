"""Opt-in stock selection on a witnessed AMS insertion; existing journal/publisher own the rest.

No scheduler or second usage book. SQL row locks arbitrate stock claims on
PostgreSQL; the existing SQLite write-lock helper serialises read/choose/write.
"""

import logging
from datetime import datetime, timezone

from pydantic import ValidationError
from sqlalchemy import func, select

from backend.app.core.database import take_write_lock
from backend.app.models.printer import Printer
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.schemas.auto_stock_spool import AutoStockSpoolPolicy, StockSpoolGroup
from backend.app.services.ams_slot_presence import spool_present
from backend.app.services.spool_tag_matcher import is_valid_tag

logger = logging.getLogger(__name__)
NAMESPACE = "auto_stock_spool"


def policy_for(printer) -> AutoStockSpoolPolicy:
    raw = getattr(printer, "ams_policies", None)
    try:
        return AutoStockSpoolPolicy.model_validate(raw.get(NAMESPACE, {}) if isinstance(raw, dict) else {})
    except ValidationError:
        return AutoStockSpoolPolicy()  # unreadable persisted policy never admits a claim


def _external_feed_loaded(state, global_tray: int) -> bool:
    # H2's normalized per-extruder snow distinguishes Ext-R (255) from the
    # legacy tray_now=255 sentinel for an unloaded single-nozzle machine.
    snow = getattr(state, "h2d_extruder_snow", {}) or {}
    return global_tray in snow.values() or (global_tray == 254 and getattr(state, "tray_now", None) == 254)


def available_filters():
    return [
        Spool.archived_at.is_(None),
        # The ordinary internal create/bulk-create API leaves this optional
        # historical marker NULL. Full stock is still represented by zero
        # actual consumption and no use history below; an explicit False
        # continues to exclude a spool entered as partial.
        Spool.added_full.is_not(False),
        Spool.weight_used == 0,
        Spool.label_weight > 0,
        Spool.filament_diameter == "1.75",
        func.coalesce(Spool.extra_colors, "") == "",
        Spool.last_used.is_(None),
        func.coalesce(Spool.tag_uid, "").in_(["", "0000000000000000"]),
        func.coalesce(Spool.tray_uuid, "").in_(["", "00000000000000000000000000000000"]),
        ~select(SpoolAssignment.id).where(SpoolAssignment.spool_id == Spool.id).exists(),
    ]


def group_columns():
    return {
        "material": Spool.material,
        "rgba": func.upper(Spool.rgba),
        "brand": func.coalesce(Spool.brand, ""),
        "subtype": func.coalesce(Spool.subtype, ""),
        "filament_family_id": func.coalesce(Spool.filament_family_id, ""),
        "label_weight": Spool.label_weight,
    }


async def available_groups(db):
    columns = group_columns()
    rows = (
        await db.execute(
            select(*(c.label(k) for k, c in columns.items()), func.count().label("available_count"))
            .where(*available_filters(), Spool.rgba.is_not(None))
            .group_by(*columns.values())
            .order_by(columns["material"], columns["brand"])
        )
    ).mappings()
    groups = []
    for row in rows:
        try:
            StockSpoolGroup.model_validate(dict(row))
        except ValidationError:
            continue
        groups.append(dict(row))
    return groups


async def claim_on_insertion(
    db, *, printer_id: int, event: dict, manager, external_runout_id: int | None = None
) -> dict:
    """One already-deduplicated, locally witnessed insertion; commit with its journal boundary.

    An existing assignment wins unless THIS slot has an open, unambiguous
    runout of THAT spool. Colour/profile matching for dispatch is deliberately
    not consulted: ignoring a colour in a job is not permission to choose a
    different physical stock group.
    """
    from backend.app.api.routes.inventory import _find_tray_in_ams_data
    from backend.app.api.routes.settings import get_setting
    from backend.app.models.print_usage_event import (
        EVENT_RESUME,
        EVENT_RUNOUT,
        EVENT_SPOOL_LOADED,
        KIND_AUTOSWITCH,
        KIND_EXTERNAL,
        KIND_PAUSE,
    )
    from backend.app.services.print_usage_journal import active_archive_id, load_events, note_assignment_change

    ams_id, tray_id = event["ams_id"], event["tray_id"]
    state = manager.get_status(printer_id)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if (
        not state
        or not state.connected
        or state.connection_generation != event["generation"]
        or not 0 <= (now - event["observed_at"]).total_seconds() <= 30
    ):
        return {"reason": "stale_insertion"}
    external = external_runout_id is not None and ams_id == 255 and tray_id in (0, 1)

    def loading_tray(current):
        if external:
            return next(
                (
                    t
                    for t in current.raw_data.get("vt_tray", []) or []
                    if isinstance(t, dict) and t.get("id") in (254 + tray_id, str(254 + tray_id))
                ),
                None,
            )
        return _find_tray_in_ams_data(current.raw_data.get("ams", []), ams_id, tray_id)

    tray = loading_tray(state)
    if (
        not tray
        or (
            external
            and (
                state.state != "RUNNING" or not _external_feed_loaded(state, 254 + tray_id) or not tray.get("tray_type")
            )
        )
        or (not external and (ams_id >= 254 or spool_present(tray) is not True))
    ):
        return {"reason": "presence_unknown"}
    if is_valid_tag(tray.get("tag_uid"), tray.get("tray_uuid")):
        return {"reason": "rfid_priority"}
    await take_write_lock(db, Printer.__table__, printer_id)
    printer = (await db.execute(select(Printer).where(Printer.id == printer_id).with_for_update())).scalar_one_or_none()
    policy = policy_for(printer)
    if not printer or printer.archived or not printer.is_active or not policy.enabled:
        return {"reason": "policy_off"}
    if (await get_setting(db, "spoolman_enabled") or "").lower() == "true":
        return {"reason": "spoolman_enabled"}
    existing = (
        await db.execute(
            select(SpoolAssignment).where(
                SpoolAssignment.printer_id == printer_id,
                SpoolAssignment.ams_id == ams_id,
                SpoolAssignment.tray_id == tray_id,
            )
        )
    ).scalar_one_or_none()
    if external and not existing:
        return {"reason": "assignment_priority"}
    if existing:
        # A deliberate pre-assignment or a manual answer after this signal wins.
        if not existing.fingerprint_type or existing.created_at > event["observed_at"]:
            return {"reason": "assignment_priority"}
        archive_id = await active_archive_id(db, printer_id)
        events = await load_events(db, printer_id, archive_id) if archive_id else []
        global_tray = 254 + tray_id if external else ams_id if ams_id >= 128 else ams_id * 4 + tray_id
        last = next(
            (
                e
                for e in reversed(events)
                if e.global_tray_id == global_tray and e.event in (EVENT_RUNOUT, EVENT_SPOOL_LOADED)
            ),
            None,
        )
        if (
            not last
            or last.event != EVENT_RUNOUT
            or last.kind not in ((KIND_EXTERNAL,) if external else (KIND_PAUSE, KIND_AUTOSWITCH))
            or last.spool_id != existing.spool_id
            # Manual re-linking of the same partial reel deliberately leaves
            # the runout open. Its assignment survives deferred fingerprint
            # filling, but a subsequent, independent runout can still replace it.
            or existing.created_at > last.created_at
        ):
            return {"reason": "assignment_priority"}
        if external and (
            last.id != external_runout_id
            or archive_id != event["archive_id"]
            or any(e.event == EVENT_RESUME and e.id > last.id for e in events)
        ):
            return {"reason": "assignment_priority"}
    columns = group_columns()
    group = policy.group.model_dump()
    if external:
        outgoing = (await db.execute(select(Spool).where(Spool.id == existing.spool_id))).scalar_one_or_none()
        if outgoing is None or any(
            ((getattr(outgoing, k) or "").upper() if k == "rgba" else (getattr(outgoing, k) or "")) != v
            for k, v in group.items()
        ):
            return {"reason": "assignment_priority"}
    spool = (
        await db.execute(
            select(Spool)
            .where(*available_filters(), *(columns[k] == v for k, v in group.items()))
            .order_by(Spool.created_at, Spool.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if spool is None:
        return {"reason": "no_full_stock"}
    # Recheck the live connection immediately before writing; a reconnect while
    # waiting for SQL must not turn its old insertion into a new assignment.
    state = manager.get_status(printer_id)
    if (
        not state
        or not state.connected
        or state.connection_generation != event["generation"]
        or not 0 <= (datetime.now(timezone.utc).replace(tzinfo=None) - event["observed_at"]).total_seconds() <= 30
    ):
        return {"reason": "stale_insertion"}
    tray = loading_tray(state)
    if (
        not tray
        or (
            external
            and (
                state.state != "RUNNING" or not _external_feed_loaded(state, 254 + tray_id) or not tray.get("tray_type")
            )
        )
        or (not external and spool_present(tray) is not True)
    ):
        return {"reason": "presence_unknown"}
    if is_valid_tag(tray.get("tag_uid"), tray.get("tray_uuid")):
        return {"reason": "rfid_priority"}
    if external and await active_archive_id(db, printer_id) != event["archive_id"]:
        return {"reason": "no_active_print"}
    if existing:
        await db.delete(existing)
        await db.flush()
    assignment = SpoolAssignment(
        spool_id=spool.id,
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        fingerprint_color=tray.get("tray_color", ""),
        # Preserve unknown firmware identity. A blank fingerprint is the
        # existing deferred-configuration marker used by manual pre-assignment:
        # the next material report replays our slot plan instead of unlinking
        # the stock claim on an intermediate default profile.
        fingerprint_type=tray.get("tray_type") or "",
        created_at=now,
    )
    db.add(assignment)
    await db.flush()
    # The existing journal's writer commits the replacement and its boundary
    # together when a runout exists. Failure is NOT swallowed on this auto path.
    await note_assignment_change(
        db,
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        spool_id=spool.id,
        layer_num=last.layer_num if external else getattr(state, "layer_num", 0),
    )
    await db.commit()
    logger.info("Stock insertion: assigned spool %s to printer %s AMS%s-T%s", spool.id, printer_id, ams_id, tray_id)
    return {"reason": "assigned", "spool_id": spool.id}


async def claim_on_external_resume(db, *, printer_id: int, event: dict, manager) -> dict:
    """Declared new-full replacement after a witnessed resume, not detection of an insertion.

    External holders expose no reel identity/presence edge. The opted-in policy
    treats a confirmed external runout followed by resume as a new full reel.
    The existing journal is the durable one-shot fence, including after restart.
    """
    from backend.app.models.print_usage_event import EVENT_RESUME, EVENT_RUNOUT, EVENT_SPOOL_LOADED, KIND_EXTERNAL
    from backend.app.services.print_usage_journal import active_archive_id, load_events

    archive_id = await active_archive_id(db, printer_id)
    if archive_id is None:
        return {"reason": "no_active_print"}
    events = await load_events(db, printer_id, archive_id)
    last_resume = max((e.id for e in events if e.event == EVENT_RESUME), default=0)
    latest_by_tray = {}
    for row in events:
        if row.global_tray_id in (254, 255) and row.event in (EVENT_RUNOUT, EVENT_SPOOL_LOADED):
            latest_by_tray[row.global_tray_id] = row
    runouts = [
        row
        for row in latest_by_tray.values()
        if row.event == EVENT_RUNOUT and row.kind == KIND_EXTERNAL and row.spool_id and row.id > last_resume
    ]
    if len(runouts) != 1:
        return {"reason": "no_unambiguous_external_runout"}
    runout = runouts[0]
    state = manager.get_status(printer_id)
    # Explicitly loading an AMS after runout does not mean an external reel was
    # replaced. The runout's full code names the holder; never infer its side
    # from the filename, colour, active extruder or a default external index.
    if state is None or not _external_feed_loaded(state, runout.global_tray_id):
        return {"reason": "external_feed_not_confirmed"}
    outcome = await claim_on_insertion(
        db,
        printer_id=printer_id,
        event={**event, "ams_id": 255, "tray_id": runout.global_tray_id - 254, "archive_id": archive_id},
        manager=manager,
        external_runout_id=runout.id,
    )
    return {**outcome, "ams_id": 255, "tray_id": runout.global_tray_id - 254}
