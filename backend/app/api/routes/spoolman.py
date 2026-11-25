"""Spoolman integration API routes."""

import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.api.routes._spoolman_helpers import _map_spoolman_spool
from backend.app.api.routes.spoolman_inventory import _clear_stale_tag_links
from backend.app.core.auth import RequireAdmin, RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.websocket import ws_manager
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.models.user import User
from backend.app.schemas.spoolman import SlotAmsId, SlotTrayId
from backend.app.services import spoolman_binding_fence
from backend.app.services.ams_slot_presence import spool_present, tray_definitely_empty
from backend.app.services.printer_manager import printer_manager
from backend.app.services.spoolman import (
    ArchivedTagIndex,
    NativeTagConflict,
    SpoolmanClientError,
    SpoolmanNotFoundError,
    SpoolmanUnavailableError,
    close_spoolman_client,
    get_spoolman_client,
    init_spoolman_client,
    native_tag_uids,
)
from backend.app.services.spoolman_kprofile_link import resolve_spoolman_slot_kprofile
from backend.app.services.spoolman_slot_ledger import write_sync_results
from backend.app.services.spoolman_weights import spool_net_weight
from backend.app.services.tag_conflict import native_tag_refused
from backend.app.utils.slot_nozzle import slot_nozzle

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/spoolman", tags=["spoolman"])


class SpoolmanStatus(BaseModel):
    """Spoolman connection status."""

    enabled: bool
    connected: bool
    url: str | None
    # This Spoolman links tags natively (0.27+): Settings offers the copy of
    # extra.tag into them. Decided by asking the server, never by a version.
    native_tags: bool = False


class SkippedSpool(BaseModel):
    """Information about a skipped spool during sync."""

    location: str
    reason: Literal["No RFID tag and no slot assignment"]
    filament_type: str | None = None
    color: str | None = None


class SyncResult(BaseModel):
    """Result of a Spoolman sync operation."""

    success: bool
    synced_count: int
    skipped_count: int = 0
    skipped: list[SkippedSpool] = []
    errors: list[str]


async def get_spoolman_settings(db: AsyncSession) -> dict:
    """Get Spoolman settings from database.

    Returns:
        Dict with keys: enabled, url, sync_mode, disable_weight_sync
    """
    settings = {
        "enabled": False,
        "url": "",
        "sync_mode": "auto",
        "disable_weight_sync": False,
    }

    result = await db.execute(select(Settings))
    for setting in result.scalars().all():
        if setting.key == "spoolman_enabled":
            settings["enabled"] = setting.value.lower() == "true"
        elif setting.key == "spoolman_url":
            settings["url"] = setting.value
        elif setting.key == "spoolman_sync_mode":
            settings["sync_mode"] = setting.value
        elif setting.key == "spoolman_disable_weight_sync":
            settings["disable_weight_sync"] = setting.value.lower() == "true"

    return settings


@router.get("/status", response_model=SpoolmanStatus)
async def get_spoolman_status(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """Get Spoolman integration status.

    ``connected`` answers "does the CONFIGURED Spoolman respond?", which means
    asking it (upstream 4e40a502). It used to answer "has an earlier request
    left a client object behind?" — some twenty call sites build one lazily,
    and saving the Settings page builds one as a side effect, so the flag
    followed which page had loaded first. The UI reads it twice (Connect only
    while disconnected, the AMS sync only while connected), so enabling
    Spoolman there showed a sync that then failed on every slot. Resolved the
    way every other route resolves a client, stale-URL check included, and
    not probed at all while the integration is off.
    """
    sm = await get_spoolman_settings(db)
    enabled, url = sm["enabled"], sm["url"]

    connected = False
    native_tags = False
    if enabled and url:
        client = await get_spoolman_client()
        if not client or client.base_url != url.rstrip("/"):
            try:
                client = await init_spoolman_client(url)
            except ValueError as exc:
                # The admin's to correct — and actionable only if the log says so.
                logger.warning("Spoolman URL %r rejected by SSRF guard during status check: %s", url, exc)
                client = None
            except Exception as exc:  # noqa: BLE001 — a status poll reports, it does not fail
                # Replacing a client closes the previous one, and httpx's
                # aclose() may raise. A poll answering 500 every 30 s is worse
                # than one reporting what is true: Spoolman could not be reached.
                logger.warning("Could not open a Spoolman client for %r during status check: %s", url, exc)
                client = None
        if client:
            connected = await client.health_check()
            # Asked once per client and remembered: the 30-second poll costs nothing more.
            native_tags = bool(connected) and await client.has_tag_api() is True

    return SpoolmanStatus(
        enabled=enabled,
        connected=connected,
        url=url if url else None,
        native_tags=native_tags,
    )


@router.post("/connect", dependencies=[RequireAdmin()])
async def connect_spoolman(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Connect to Spoolman server using configured URL."""
    sm = await get_spoolman_settings(db)
    enabled, url = sm["enabled"], sm["url"]

    if not enabled:
        raise HTTPException(status_code=400, detail="Spoolman integration is not enabled")

    if not url:
        raise HTTPException(status_code=400, detail="Spoolman URL is not configured")

    try:
        client = await init_spoolman_client(url)
        connected = await client.health_check()

        if not connected:
            raise HTTPException(
                status_code=503,
                detail=f"Could not connect to Spoolman at {url}",
            )

        # Ensure the 'tag' extra field exists for RFID/UUID storage
        field_ok = await client.ensure_tag_extra_field()
        if not field_ok:
            logger.error("Spoolman tag extra field registration failed — NFC tag links may not persist")
        # Register slicer-preset extra fields (Spoolman rejects unknown extra keys).
        for field_name in ("bambu_slicer_filament", "bambu_slicer_filament_name"):
            if not await client.ensure_extra_field(field_name):
                logger.warning(
                    "Spoolman extra field %r registration failed — spool slicer-preset edits will return 502",
                    field_name,
                )

        return {"success": True, "message": f"Connected to Spoolman at {url}"}
    except ValueError as exc:
        logger.warning("Spoolman URL rejected: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as e:
        logger.error("Failed to connect to Spoolman: %s", e)
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/disconnect", dependencies=[RequireAdmin()])
async def disconnect_spoolman(
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Disconnect from Spoolman server."""
    await close_spoolman_client()
    return {"success": True, "message": "Disconnected from Spoolman"}


async def _announce_slot(printer_id: int, ams_id: int, tray_id: int) -> None:
    await ws_manager.broadcast(
        {"type": "spool_assignment_changed", "printer_id": printer_id, "ams_id": ams_id, "tray_id": tray_id}
    )


async def _read_slot_ledger(db: AsyncSession, printer_id: int | None = None) -> dict[tuple[int, int, int], int] | None:
    """``(printer_id, ams_id, tray_id)`` → spool id, or ``None`` when it could not be read."""
    try:
        query = select(SpoolmanSlotAssignment)
        if printer_id is not None:
            query = query.where(SpoolmanSlotAssignment.printer_id == printer_id)
        rows = (await db.execute(query)).scalars().all()
        return {(row.printer_id, row.ams_id, row.tray_id): row.spoolman_spool_id for row in rows}
    except Exception as e:
        logger.warning("Could not load Spoolman slot assignments: %s", e)
        return None


def _being_deleted_text(refused, where: str) -> str:
    """A sync's line for a number the fence refused (C047 S32, S37)."""
    if refused.created:
        return f"{where}: spool #{refused.number} was created in Spoolman but not bound because of a concurrent delete"
    return f"{where}: spool #{refused.number} is being deleted; not synced"


async def _write_and_announce_sync(
    db: AsyncSession,
    read: dict[tuple[int, int, int], int] | None,
    changes: list[tuple[int, int, int, int]],
    empties: list[tuple[int, int, int]],
    *,
    issued: int | None = None,
) -> list[str]:
    """Persist what a sync resolved and tell open browsers which slots moved.

    The sync endpoints are what maintains ``spoolman_slot_assignments``, and the
    printer card reads those rows — without an event every other tab kept the
    previous spool (upstream 7363d5fd). The write is conditional on the ledger
    the sync READ (``services/spoolman_slot_ledger``, upstream fcc20cb62), so
    what is announced is what the database says moved, after its commit — never
    a comparison with that snapshot, which a hand assignment made meanwhile has
    already left behind. A failed write stays silent.

    ``read`` is keyed ``(printer_id, ams_id, tray_id)``; ``None`` = unreadable.
    One printer at a time, each under its own assignment lock. Returns errors.
    """
    errors: list[str] = []
    for printer_id in dict.fromkeys([c[0] for c in changes] + [e[0] for e in empties]):
        try:
            moved = await write_sync_results(
                db,
                printer_id=printer_id,
                read=None if read is None else {(a, t): s for (p, a, t), s in read.items() if p == printer_id},
                changes=[(a, t, s) for p, a, t, s in changes if p == printer_id],
                empties=[(a, t) for p, a, t in empties if p == printer_id],
                issued=issued,
            )
        except spoolman_binding_fence.SpoolBeingDeleted as refused:
            errors.append(_being_deleted_text(refused, f"printer {printer_id}"))
            continue
        except Exception as e:
            logger.error("Error persisting Spoolman slot assignments for printer %s: %s", printer_id, e)
            errors.append(f"Failed to persist slot assignments: {type(e).__name__}")
            continue
        for ams_id, tray_id in moved:
            await _announce_slot(printer_id, ams_id, tray_id)
    return errors


@router.post("/sync/{printer_id}", response_model=SyncResult)
async def sync_printer_ams(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """A manual AMS sync owns every number it proves, to its batch commit (C047 S32)."""
    issued = spoolman_binding_fence.ticket()
    with spoolman_binding_fence.tag_operation(issued=issued):
        return await _sync_printer_ams(printer_id, db, _, issued=issued)


async def _sync_printer_ams(printer_id: int, db: AsyncSession, _, *, issued: int):
    """Sync AMS data from a specific printer to Spoolman."""
    # Check if Spoolman is enabled and connected.
    # ``disable_weight_sync`` is deprecated (#1119): weight is now always
    # owned by per-print 3MF tracking, so the manual sync below forces
    # ``disable_weight_sync=True`` regardless of the stored setting.
    sm = await get_spoolman_settings(db)
    enabled, url = sm["enabled"], sm["url"]
    if not enabled:
        raise HTTPException(status_code=400, detail="Spoolman integration is not enabled")

    client = await get_spoolman_client()
    if not client:
        # Try to connect
        if url:
            client = await init_spoolman_client(url)
        else:
            raise HTTPException(status_code=400, detail="Spoolman URL is not configured")

    if not await client.health_check():
        raise HTTPException(status_code=503, detail="Spoolman is not reachable")

    # Get printer info
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    # Get current printer state with AMS data
    state = printer_manager.get_status(printer_id)
    if not state:
        raise HTTPException(status_code=404, detail="Printer not connected")

    if not state.raw_data:
        raise HTTPException(status_code=400, detail="No AMS data available")

    ams_data = state.raw_data.get("ams")
    if not ams_data:
        raise HTTPException(
            status_code=400,
            detail="No AMS data in printer state. Try triggering a slot re-read on the printer.",
        )

    # Sync each AMS tray to Spoolman
    synced = 0
    skipped: list[SkippedSpool] = []
    errors = []

    from backend.app.api.routes.settings import get_setting

    _auto_add_raw = await get_setting(db, "auto_add_unknown_rfid")
    auto_add_unknown_rfid = _auto_add_raw is None or _auto_add_raw.lower() == "true"

    # Handle different AMS data structures
    # Traditional AMS: list of {"id": N, "tray": [...]} dicts
    # H2D/newer printers: dict with different structure
    ams_units = []
    if isinstance(ams_data, list):
        ams_units = ams_data
    elif isinstance(ams_data, dict):
        # H2D format: check for "ams" key containing list, or "tray" key directly
        if "ams" in ams_data and isinstance(ams_data["ams"], list):
            ams_units = ams_data["ams"]
        elif "tray" in ams_data:
            # Single AMS unit format - wrap in list
            ams_units = [{"id": 0, "tray": ams_data.get("tray", [])}]
        else:
            logger.info("AMS dict keys for debugging: %s", list(ams_data.keys()))

    if not ams_units:
        raise HTTPException(
            status_code=400,
            detail=(
                "AMS data format not supported. Keys: "
                f"{list(ams_data.keys()) if isinstance(ams_data, dict) else type(ams_data).__name__}"
            ),
        )

    # OPTIMIZATION: Fetch all spools once before processing trays
    # This eliminates redundant API calls (one per tray) when syncing multiple trays
    logger.debug("[Printer %s] Fetching spools cache for sync...", printer.name)
    try:
        cached_spools = await client.get_spools()
        logger.debug("[Printer %s] Cached %d spools for batch sync", printer.name, len(cached_spools))
    except Exception as e:
        logger.error("[Printer %s] Failed to fetch spools cache after retries: %s", printer.name, e)
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to Spoolman after multiple retries: {str(e)}",
        )
    archived_reels = ArchivedTagIndex(client)

    # Load inventory weights as fallback (when AMS MQTT data lacks remain values)
    inv_weights: dict[tuple[int, int], float] = {}
    try:
        assign_result = await db.execute(
            select(SpoolAssignment)
            .options(selectinload(SpoolAssignment.spool))
            .where(SpoolAssignment.printer_id == printer_id)
        )
        for assignment in assign_result.scalars().all():
            spool = assignment.spool
            if spool and spool.label_weight > 0:
                remaining = max(0.0, spool.label_weight - (spool.weight_used or 0))
                inv_weights[(assignment.ams_id, assignment.tray_id)] = remaining
    except Exception as e:
        logger.debug("Could not load inventory weights for printer %s: %s", printer_id, e)

    # Load existing Spoolman slot assignments for the no-RFID fallback path
    ledger_read = await _read_slot_ledger(db, printer_id)
    spoolman_slot_map: dict[tuple[int, int], int] = {(a, t): s for (_p, a, t), s in (ledger_read or {}).items()}

    slot_changes: list[tuple[int, int, int]] = []  # (ams_id, tray_id, spoolman_spool_id)
    empty_slots: list[tuple[int, int]] = []  # (ams_id, tray_id) now empty

    # ⚠️ A slot that reports empty while the printer is RUNNING/PAUSE is a
    # filament runout, not a spool somebody removed: the spool is still in the
    # bay, just consumed. This ledger is how a tag-less spool is resolved at
    # completion, so dropping the row mid-print loses the runout segment's usage
    # entirely. The next sync with the printer idle still clears it.
    printing_now = (getattr(state, "state", "") or "").upper() in ("RUNNING", "PAUSE")

    for ams_unit in ams_units:
        if not isinstance(ams_unit, dict):
            continue

        ams_id = int(ams_unit.get("id", 0))
        trays = ams_unit.get("tray", [])

        for tray_data in trays:
            if not isinstance(tray_data, dict):
                continue

            tray_id_raw = int(tray_data.get("id", 0))
            tray = client.parse_ams_tray(ams_id, tray_data)
            if not tray:
                # A tray with no type is "empty" to parse_ams_tray, and a tag-less
                # spool has none until configured: the presence bit decides
                # (upstream #3100), as it does for the built-in inventory.
                if not printing_now and spool_present(tray_data) is not True:
                    empty_slots.append((ams_id, tray_id_raw))
                continue

            spool_tag = (
                tray.tray_uuid
                if tray.tray_uuid and tray.tray_uuid != "00000000000000000000000000000000"
                else tray.tag_uid
            )
            if spool_tag and await archived_reels.holds_archived_reel(spool_tag, cached_spools, tray.tag_uid or ""):
                # A retired reel still in the slot — not new, not to be re-created.
                continue

            hint = spoolman_slot_map.get((ams_id, tray.tray_id)) if not spool_tag else None

            try:
                inv_remaining = inv_weights.get((ams_id, tray.tray_id))
                # Per-print tracking owns weight updates (#1119); manual sync
                # only refreshes spool metadata + slot assignments here.
                sync_result = await client.sync_ams_tray(
                    tray,
                    printer.name,
                    disable_weight_sync=True,
                    cached_spools=cached_spools,
                    inventory_remaining=inv_remaining,
                    spoolman_spool_id_hint=hint,
                    auto_add_unknown_rfid=auto_add_unknown_rfid,
                    db=db,
                )
                if sync_result:
                    synced += 1
                    if sync_result.get("id"):
                        slot_changes.append((ams_id, tray.tray_id, sync_result["id"]))
                        spool_exists = any(s.get("id") == sync_result["id"] for s in cached_spools)
                        if not spool_exists:
                            cached_spools.append(sync_result)
                            logger.debug("Added newly created spool %s to cache", sync_result["id"])
                    logger.info(
                        "Synced %s from %s AMS %s tray %s", tray.tray_sub_brands, printer.name, ams_id, tray.tray_id
                    )
                elif spool_tag and not auto_add_unknown_rfid:
                    skipped.append(
                        SkippedSpool(
                            location=f"AMS {ams_id} T{tray.tray_id}",
                            reason="Auto-add disabled; add to inventory manually",
                            filament_type=tray.tray_type or None,
                            color=tray.tray_color[:6] if tray.tray_color else None,
                        )
                    )
                elif spool_tag:
                    errors.append(f"Spool not found in Spoolman: AMS {ams_id}:{tray.tray_id}")
                elif not hint:
                    skipped.append(
                        SkippedSpool(
                            location=f"AMS {ams_id} T{tray.tray_id}",
                            reason="No RFID tag and no slot assignment",
                            filament_type=tray.tray_type or None,
                            color=tray.tray_color[:6] if tray.tray_color else None,
                        )
                    )
            except spoolman_binding_fence.SpoolBeingDeleted as refused:
                errors.append(_being_deleted_text(refused, f"AMS {ams_id} tray {tray.tray_id}"))
            except Exception as e:
                error_msg = f"Error syncing AMS {ams_id} tray {tray.tray_id}: {e}"
                logger.error(error_msg)
                errors.append(error_msg)

    # Persist slot assignment changes to the local table
    errors += await _write_and_announce_sync(
        db,
        ledger_read,
        [(printer_id, a, t, s) for a, t, s in slot_changes],
        [(printer_id, a, t) for a, t in empty_slots],
        issued=issued,
    )

    return SyncResult(
        success=len(errors) == 0,
        synced_count=synced,
        skipped_count=len(skipped),
        skipped=skipped,
        errors=errors,
    )


@router.post("/sync-all", response_model=SyncResult)
async def sync_all_printers(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """A manual AMS sync of every printer owns every number it proves, to its batch commit (C047 S32)."""
    issued = spoolman_binding_fence.ticket()
    with spoolman_binding_fence.tag_operation(issued=issued):
        return await _sync_all_printers(db, _, issued=issued)


async def _sync_all_printers(db: AsyncSession, _, *, issued: int):
    """Sync AMS data from all connected printers to Spoolman."""
    # Check if Spoolman is enabled.
    # ``disable_weight_sync`` is deprecated (#1119): weight is now always
    # owned by per-print 3MF tracking, so this manual sync-all path forces
    # ``disable_weight_sync=True`` regardless of the stored setting.
    sm = await get_spoolman_settings(db)
    enabled, url = sm["enabled"], sm["url"]
    if not enabled:
        raise HTTPException(status_code=400, detail="Spoolman integration is not enabled")

    client = await get_spoolman_client()
    if not client:
        if url:
            client = await init_spoolman_client(url)
        else:
            raise HTTPException(status_code=400, detail="Spoolman URL is not configured")

    if not await client.health_check():
        raise HTTPException(status_code=503, detail="Spoolman is not reachable")

    # Get all active printers
    result = await db.execute(select(Printer).where(Printer.is_active.is_(True)).where(Printer.archived.is_(False)))
    printers = result.scalars().all()

    total_synced = 0
    all_skipped: list[SkippedSpool] = []
    all_errors = []

    from backend.app.api.routes.settings import get_setting

    _auto_add_raw = await get_setting(db, "auto_add_unknown_rfid")
    auto_add_unknown_rfid = _auto_add_raw is None or _auto_add_raw.lower() == "true"

    # OPTIMIZATION: Fetch all spools once before processing ALL printers/trays
    # This eliminates redundant API calls across all printers
    logger.debug("Fetching spools cache for sync-all operation...")
    try:
        cached_spools = await client.get_spools()
        logger.debug("Cached %d spools for batch sync across %d printers", len(cached_spools), len(printers))
    except Exception as e:
        logger.error("Failed to fetch spools cache after retries: %s", e)
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to Spoolman after multiple retries: {str(e)}",
        )
    archived_reels = ArchivedTagIndex(client)

    # Load inventory assignments for weight fallback (when AMS MQTT data lacks remain values)
    # Key: (printer_id, ams_id, tray_id) → remaining_weight in grams
    inventory_weights: dict[tuple[int, int, int], float] = {}
    try:
        assign_result = await db.execute(select(SpoolAssignment).options(selectinload(SpoolAssignment.spool)))
        for assignment in assign_result.scalars().all():
            spool = assignment.spool
            if spool and spool.label_weight > 0:
                remaining = max(0.0, spool.label_weight - (spool.weight_used or 0))
                inventory_weights[(assignment.printer_id, assignment.ams_id, assignment.tray_id)] = remaining
    except Exception as e:
        logger.debug("Could not load inventory assignments for weight fallback: %s", e)

    # Load all Spoolman slot assignments for the no-RFID fallback
    # Key: (printer_id, ams_id, tray_id) → spoolman_spool_id
    ledger_read = await _read_slot_ledger(db)
    all_slot_map: dict[tuple[int, int, int], int] = ledger_read or {}

    # Collect slot changes across all printers for a single DB write at the end
    all_slot_changes: list[tuple[int, int, int, int]] = []  # (printer_id, ams_id, tray_id, spool_id)
    all_empty_slots: list[tuple[int, int, int]] = []  # (printer_id, ams_id, tray_id)

    for printer in printers:
        state = printer_manager.get_status(printer.id)
        if not state or not state.raw_data:
            continue

        # ⚠️ A slot that reports empty while the printer is RUNNING/PAUSE is a
        # filament runout, not a spool somebody removed: the spool is still in the
        # bay, just consumed. This ledger is how a tag-less spool is resolved at
        # completion, so dropping the row mid-print loses the runout segment's usage
        # entirely. The next sync with the printer idle still clears it.
        printing_now = (getattr(state, "state", "") or "").upper() in ("RUNNING", "PAUSE")

        ams_data = state.raw_data.get("ams")
        if not ams_data:
            continue

        # Handle different AMS data structures
        # Traditional AMS: list of {"id": N, "tray": [...]} dicts
        # H2D/newer printers: dict with different structure
        ams_units = []
        if isinstance(ams_data, list):
            ams_units = ams_data
        elif isinstance(ams_data, dict):
            # H2D format: check for "ams" key containing list, or "tray" key directly
            if "ams" in ams_data and isinstance(ams_data["ams"], list):
                ams_units = ams_data["ams"]
            elif "tray" in ams_data:
                # Single AMS unit format - wrap in list
                ams_units = [{"id": 0, "tray": ams_data.get("tray", [])}]
            else:
                logger.debug("Printer %s AMS dict keys: %s", printer.name, list(ams_data.keys()))

        if not ams_units:
            logger.debug("Printer %s has no AMS units to sync (type: %s)", printer.name, type(ams_data).__name__)
            continue

        for ams_unit in ams_units:
            if not isinstance(ams_unit, dict):
                logger.debug("Skipping non-dict AMS unit: %s", type(ams_unit))
                continue

            ams_id = int(ams_unit.get("id", 0))
            trays = ams_unit.get("tray", [])

            for tray_data in trays:
                if not isinstance(tray_data, dict):
                    continue

                tray_id_raw = int(tray_data.get("id", 0))
                tray = client.parse_ams_tray(ams_id, tray_data)
                if not tray:
                    # Presence bit first — see sync_printer (upstream #3100).
                    if not printing_now and spool_present(tray_data) is not True:
                        all_empty_slots.append((printer.id, ams_id, tray_id_raw))
                    continue

                spool_tag = (
                    tray.tray_uuid
                    if tray.tray_uuid and tray.tray_uuid != "00000000000000000000000000000000"
                    else tray.tag_uid
                )
                if spool_tag and await archived_reels.holds_archived_reel(spool_tag, cached_spools, tray.tag_uid or ""):
                    # A retired reel still in the slot — not new, not to be re-created.
                    continue

                hint = all_slot_map.get((printer.id, ams_id, tray.tray_id)) if not spool_tag else None

                try:
                    inv_remaining = inventory_weights.get((printer.id, ams_id, tray.tray_id))
                    # Per-print tracking owns weight updates (#1119); manual
                    # sync-all only refreshes spool metadata + slot assignments.
                    sync_result = await client.sync_ams_tray(
                        tray,
                        printer.name,
                        disable_weight_sync=True,
                        cached_spools=cached_spools,
                        inventory_remaining=inv_remaining,
                        spoolman_spool_id_hint=hint,
                        auto_add_unknown_rfid=auto_add_unknown_rfid,
                        db=db,
                    )
                    if sync_result:
                        total_synced += 1
                        if sync_result.get("id"):
                            all_slot_changes.append((printer.id, ams_id, tray.tray_id, sync_result["id"]))
                            spool_exists = any(s.get("id") == sync_result["id"] for s in cached_spools)
                            if not spool_exists:
                                cached_spools.append(sync_result)
                                logger.debug("Added newly created spool %s to cache", sync_result["id"])
                    elif spool_tag and not auto_add_unknown_rfid:
                        all_skipped.append(
                            SkippedSpool(
                                location=f"{printer.name} AMS {ams_id} T{tray.tray_id}",
                                reason="Auto-add disabled; add to inventory manually",
                                filament_type=tray.tray_type or None,
                                color=tray.tray_color[:6] if tray.tray_color else None,
                            )
                        )
                    elif spool_tag:
                        all_errors.append(f"Spool not found in Spoolman: {printer.name} AMS {ams_id}:{tray.tray_id}")
                    elif not hint:
                        all_skipped.append(
                            SkippedSpool(
                                location=f"{printer.name} AMS {ams_id} T{tray.tray_id}",
                                reason="No RFID tag and no slot assignment",
                                filament_type=tray.tray_type or None,
                                color=tray.tray_color[:6] if tray.tray_color else None,
                            )
                        )
                except spoolman_binding_fence.SpoolBeingDeleted as refused:
                    all_errors.append(_being_deleted_text(refused, f"{printer.name} AMS {ams_id}:{tray.tray_id}"))
                except Exception as e:
                    all_errors.append(f"{printer.name} AMS {ams_id}:{tray.tray_id}: {e}")

    # Persist slot assignment changes across all printers
    all_errors += await _write_and_announce_sync(db, ledger_read, all_slot_changes, all_empty_slots, issued=issued)

    return SyncResult(
        success=len(all_errors) == 0,
        synced_count=total_synced,
        skipped_count=len(all_skipped),
        skipped=all_skipped,
        errors=all_errors,
    )


@router.get("/spools")
async def get_spools(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """Get all spools from Spoolman."""
    sm = await get_spoolman_settings(db)
    enabled, url = sm["enabled"], sm["url"]
    if not enabled:
        raise HTTPException(status_code=400, detail="Spoolman integration is not enabled")

    client = await get_spoolman_client()
    if not client:
        if url:
            client = await init_spoolman_client(url)
        else:
            raise HTTPException(status_code=400, detail="Spoolman URL is not configured")

    if not await client.health_check():
        raise HTTPException(status_code=503, detail="Spoolman is not reachable")

    spools = await client.get_spools()
    return {"spools": spools}


@router.get("/filaments")
async def get_filaments(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """Get all filaments from Spoolman."""
    sm = await get_spoolman_settings(db)
    enabled, url = sm["enabled"], sm["url"]
    if not enabled:
        raise HTTPException(status_code=400, detail="Spoolman integration is not enabled")

    client = await get_spoolman_client()
    if not client:
        if url:
            client = await init_spoolman_client(url)
        else:
            raise HTTPException(status_code=400, detail="Spoolman URL is not configured")

    if not await client.health_check():
        raise HTTPException(status_code=503, detail="Spoolman is not reachable")

    filaments = await client.get_filaments()
    return {"filaments": filaments}


class UnlinkedSpool(BaseModel):
    """A Spoolman spool that is not linked to any AMS tray."""

    id: int
    filament_name: str | None
    filament_vendor: str | None
    filament_material: str | None
    filament_color_hex: str | None
    remaining_weight: float | None
    location: str | None


@router.get("/spools/unlinked", response_model=list[UnlinkedSpool])
async def get_unlinked_spools(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """Get all Spoolman spools not currently assigned to an AMS slot."""
    sm = await get_spoolman_settings(db)
    enabled, url = sm["enabled"], sm["url"]
    if not enabled:
        raise HTTPException(status_code=400, detail="Spoolman integration is not enabled")

    client = await get_spoolman_client()
    if not client:
        if url:
            client = await init_spoolman_client(url)
        else:
            raise HTTPException(status_code=400, detail="Spoolman URL is not configured")

    if not await client.health_check():
        raise HTTPException(status_code=503, detail="Spoolman is not reachable")

    spools = await client.get_spools()

    # A spool is "assignable" iff it does not currently occupy an AMS slot.
    # Assignability is decided by the spoolman_slot_assignments ledger — NOT by
    # the presence of extra.tag. extra.tag is only an RFID/NFC matching key, and
    # OpenSpoolman writes its own NFC tag value into that same field (#1122);
    # treating any non-empty extra.tag as "linked" hid every OpenSpoolman-tagged
    # spool from this picker even when it occupied no slot. Both link_spool and
    # the AMS auto-sync upsert a row here for every occupied slot, so the ledger
    # is a complete record of what is actually assigned.
    assigned_result = await db.execute(select(SpoolmanSlotAssignment.spoolman_spool_id))
    assigned_spool_ids = set(assigned_result.scalars().all())

    unlinked = []
    for spool in spools:
        if spool["id"] in assigned_spool_ids:
            continue
        filament = spool.get("filament", {}) or {}
        unlinked.append(
            UnlinkedSpool(
                id=spool["id"],
                filament_name=filament.get("name"),
                filament_vendor=(filament.get("vendor") or {}).get("name"),
                filament_material=filament.get("material"),
                filament_color_hex=filament.get("color_hex"),
                remaining_weight=spool.get("remaining_weight"),
                location=spool.get("location"),
            )
        )

    return unlinked


@router.get("/spools/linked")
async def get_linked_spools(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """Get a map of tag -> spool_id for all Spoolman spools that have a tag assigned."""
    sm = await get_spoolman_settings(db)
    enabled, url = sm["enabled"], sm["url"]
    if not enabled:
        raise HTTPException(status_code=400, detail="Spoolman integration is not enabled")

    client = await get_spoolman_client()
    if not client:
        if url:
            client = await init_spoolman_client(url)
        else:
            raise HTTPException(status_code=400, detail="Spoolman URL is not configured")

    if not await client.health_check():
        raise HTTPException(status_code=503, detail="Spoolman is not reachable")

    spools = await client.get_spools()
    linked: dict[str, dict] = {}

    for spool in spools:
        # Check if spool has a tag in extra field
        extra = spool.get("extra", {}) or {}
        tag = extra.get("tag", "")
        if tag:
            # Remove quotes if present (JSON encoded string)
            clean_tag = tag.strip('"').upper()
            if clean_tag:
                linked[clean_tag] = {
                    "id": spool["id"],
                    "remaining_weight": spool.get("remaining_weight"),
                    # The spool's own net weight, falling back to the
                    # filament's; the key predates ``initial_weight``.
                    "filament_weight": spool_net_weight(spool),
                }

    # Native tags (Spoolman 0.27+) outrank extra.tag, as in find_spool_by_tag.
    for spool in spools:
        for uid in native_tag_uids(spool):
            linked[uid] = {
                "id": spool["id"],
                "remaining_weight": spool.get("remaining_weight"),
                "filament_weight": spool_net_weight(spool),
            }

    return {"linked": linked}


class LinkSpoolRequest(BaseModel):
    """Request to link a Spoolman spool to an AMS tag (tray_uuid or tag_uid)."""

    spool_tag: str | None = None
    tray_uuid: str | None = None
    tag_uid: str | None = None
    printer_id: int | None = None
    # The slot ledger's own range: refused before the tag is written to Spoolman.
    ams_id: SlotAmsId | None = None
    tray_id: SlotTrayId | None = None


@router.post("/spools/{spool_id}/link")
async def link_spool(
    spool_id: int,
    request: LinkSpoolRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Link a Spoolman spool to an AMS tag by setting Spoolman extra.tag."""
    issued = spoolman_binding_fence.ticket()
    sm = await get_spoolman_settings(db)
    enabled, url = sm["enabled"], sm["url"]
    if not enabled:
        raise HTTPException(status_code=400, detail="Spoolman integration is not enabled")

    client = await get_spoolman_client()
    if not client:
        if url:
            client = await init_spoolman_client(url)
        else:
            raise HTTPException(status_code=400, detail="Spoolman URL is not configured")

    if not await client.health_check():
        raise HTTPException(status_code=503, detail="Spoolman is not reachable")

    # Resolve and validate spool tag (supports tray_uuid=32 hex and tag_uid=16 hex)
    spool_tag = (request.spool_tag or request.tray_uuid or request.tag_uid or "").strip()
    if not spool_tag:
        raise HTTPException(status_code=400, detail="Missing spool tag (tray_uuid or tag_uid)")
    if len(spool_tag) not in (16, 32):
        raise HTTPException(status_code=400, detail="Invalid spool tag format (must be 16 or 32 hex characters)")
    try:
        int(spool_tag, 16)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid spool tag format (must be hex)")

    if set(spool_tag) == {"0"}:
        raise HTTPException(status_code=400, detail="Invalid spool tag format (all-zero tag is not linkable)")

    spool_tag = spool_tag.upper()

    # Validate printer context when provided, but do NOT write spool.location —
    # that field is user-managed in Spoolman. Slot assignment is stored locally.
    printer_context: tuple[int, int, int] | None = None
    if request.printer_id is not None and request.ams_id is not None and request.tray_id is not None:
        printer_result = await db.execute(select(Printer).where(Printer.id == request.printer_id))
        if not printer_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Printer not found")
        printer_context = (request.printer_id, request.ams_id, request.tray_id)

    # A link with a slot is an assignment, and a spool sits in one slot (owner
    # decision Р7): the slot it would leave must not be feeding a running print.
    # Asked before any tag is written anywhere.
    from backend.app.services import spool_single_slot

    if printer_context:
        for _other in await spool_single_slot.other_spoolman_slots(
            db, spoolman_spool_id=spool_id, keep=printer_context
        ):
            if spool_single_slot.slot_feeds_running_print(*_other):
                raise spool_single_slot.refusal(spool_single_slot.SpoolFeedsPrint(_other))
    # One tag operation for the whole re-link (C047 S27): the other holders of this
    # tag in ``extra.tag`` are found NOW, before the first mutation, and owned with the
    # spool, ascending — a refusal of any of them is 409 with nothing written.
    holders = await _extra_tag_holders(client, spool_tag, keep_spool_id=spool_id)
    with spoolman_binding_fence.tag_operation([spool_id, *holders], issued=issued):
        _vacated: list = []

        try:
            if printer_context and (await client.get_spool(spool_id)).get("archived"):
                # A link with a slot is an assignment: as the assign routes refuse,
                # and before any tag — native or extra.tag — is written (upstream
                # c2bc41a5e).
                raise HTTPException(status_code=400, detail="Cannot assign an archived spool")
            # A tray UUID also becomes a native tag (Spoolman 0.27+), linked first:
            # it is the step Spoolman can refuse, so a refusal leaves extra.tag
            # untouched. The 16-character values this route takes — a slot's
            # fallback id, the AMS's padded chip uid — are not what a reader sees
            # and stay in extra.tag alone.
            added_native = await client.link_native_tags(spool_id, [spool_tag] if len(spool_tag) == 32 else [], "bambu")
            try:
                # A reel newly linked to the spool: read it while it still carries
                # the previous extra.tag, to know which reel it was before.
                before = await client.get_spool(spool_id) if added_native else None
                await client.merge_spool_extra(spool_id, {"tag": json.dumps(spool_tag)})
            except Exception:
                await client.take_back_native_tags(spool_id, added_native)
                raise
        except NativeTagConflict as conflict:
            raise native_tag_refused("tray_uuid", conflict) from conflict
        except SpoolmanNotFoundError:
            raise HTTPException(status_code=404, detail="Spool not found in Spoolman")
        except SpoolmanClientError:
            raise HTTPException(status_code=502, detail="Spoolman rejected the request")
        except SpoolmanUnavailableError:
            raise HTTPException(status_code=503, detail="Spoolman is not reachable")

        # Upsert slot assignment locally when printer context was supplied
        if printer_context:
            p_id, a_id, t_id = printer_context
            # A binding writer (C047 §C3): the fence's guard around the slot's DML and commit.
            with spoolman_binding_fence.enter([spool_id], issued):
                try:
                    await db.execute(
                        text(
                            "INSERT INTO spoolman_slot_assignments"
                            " (printer_id, ams_id, tray_id, spoolman_spool_id)"
                            " VALUES (:printer_id, :ams_id, :tray_id, :spool_id)"
                            " ON CONFLICT(printer_id, ams_id, tray_id)"
                            " DO UPDATE SET spoolman_spool_id = excluded.spoolman_spool_id"
                        ),
                        {"printer_id": p_id, "ams_id": a_id, "tray_id": t_id, "spool_id": spool_id},
                    )
                    # Its other ledger rows go with the same commit (rows only). A slot
                    # that began feeding a print since the check above keeps its row.
                    _vacated = (
                        await spool_single_slot.release_other_spoolman_slots(
                            db, spoolman_spool_id=spool_id, keep=printer_context, refuse=False
                        )
                    ).vacated
                    await db.commit()
                except Exception as e:
                    await db.rollback()
                    # The native tag goes back (the previous reel's are still there —
                    # they are dropped only below); extra.tag stays, as the message
                    # says, and the retry it asks for links the reel again.
                    await client.take_back_native_tags(spool_id, added_native)
                    logger.error(
                        "Linked spool %s in Spoolman but failed to persist local slot assignment "
                        "(printer=%s ams=%s tray=%s): %s",
                        spool_id,
                        p_id,
                        a_id,
                        t_id,
                        e,
                    )
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "Spool linked in Spoolman but the local slot assignment could not be saved. "
                            "Please re-open the link dialog to retry."
                        ),
                    ) from e

        # The spool is THIS reel now: the reel it was linked to before no longer
        # resolves to it. A 16-character value names no physical tag here (nothing
        # was added for it), so it takes nothing away. Last, once the slot row is
        # saved: a link that failed half way keeps the spool's previous reel.
        if before is not None:
            await client.drop_previous_reel_tags(before, [spool_tag], bambu_reel=True)

        logger.info("Linked Spoolman spool %s to tag %s", spool_id, spool_tag)
        await spool_single_slot.announce(_vacated)

        # #1457: clear stale tag links on OTHER spools still claiming this exact tag.
        # A given AMS-slot tag (RFID or deterministic fallback) belongs to one
        # physical spool; without this cleanup the previous holder's extra.tag
        # keeps it visible in the hover card / fill-level lookup.
        await _clear_stale_tag_links(
            client,
            tag=spool_tag,
            keep_spool_id=spool_id,
            log_context=(
                f"printer={printer_context[0]} ams={printer_context[1]} tray={printer_context[2]}"
                if printer_context
                else "via /spools/{id}/link"
            ),
        )

        # Auto-configure AMS slot via MQTT (best-effort; tag link and slot assignment already persisted)
        if printer_context:
            p_id, a_id, t_id = printer_context
            try:
                spool_data = await client.get_spool(spool_id)
                mapped = _map_spoolman_spool(spool_data)

                mqtt_client = printer_manager.get_client(p_id)
                if mqtt_client:
                    tray_type = mapped.get("material") or ""
                    brand = mapped.get("brand") or ""
                    subtype = mapped.get("subtype") or ""
                    if brand:
                        tray_sub_brands = f"{brand} {tray_type} {subtype}".strip()
                    elif subtype:
                        tray_sub_brands = f"{tray_type} {subtype}".strip()
                    else:
                        tray_sub_brands = tray_type

                    tray_color = (mapped.get("rgba") or "808080FF").upper()
                    if len(tray_color) == 6:
                        tray_color = tray_color + "FF"

                    # Pull printer state via printer_manager (mqtt_client.printer_state
                    # was a non-existent attribute — the hasattr check silently
                    # returned None, defeating every state-based lookup below).
                    state = printer_manager.get_status(p_id)
                    # The nozzle THIS slot feeds — diameter, hotend and flow type.
                    nozzle = slot_nozzle(state, a_id, t_id)
                    nozzle_diameter = nozzle.diameter
                    slot_extruder = nozzle.extruder_or_default
                    nozzle_dia_float = nozzle.diameter_float

                    # Pick link by matching nozzle on the joined filament_calibration.
                    matching_fc = await resolve_spoolman_slot_kprofile(
                        db,
                        printer_id=p_id,
                        spoolman_spool_id=spool_id,
                        nozzle_diameter=nozzle_dia_float,
                        slot_extruder=nozzle.extruder,
                        nozzle_flow=nozzle.flow,
                    )

                    # ONE identity path (spec A §5.2): the family catalog builds the
                    # payload. The Spoolman spool's identity is the family of its
                    # linked calibration when one exists; otherwise the preset
                    # stored on the spool, otherwise a stand-in for the material —
                    # resolved inside the builder, no hand-rolled realignment.
                    from backend.app.services.ams_backup_compatibility import (  # noqa: PLC0415
                        kprofile_allowed,
                        live_tray_for,
                    )
                    from backend.app.services.slot_assignment import build_slot_assignment  # noqa: PLC0415
                    from backend.app.services.slot_assignment_publish import (  # noqa: PLC0415
                        publish_projected_slot,
                    )

                    supports_user_preset = bool(getattr(state, "support_user_preset", False))
                    # Model cache, not PrinterInfo — see configure_ams_slot.
                    printer_model = printer_manager.get_model(p_id)
                    plan = await build_slot_assignment(
                        db,
                        family_id=matching_fc.filament_id if matching_fc else None,
                        stored_preset=mapped.get("slicer_filament"),
                        material_override=tray_type,
                        color_rgba=tray_color,
                        temp_overrides=(mapped.get("nozzle_temp_min"), None),
                        printer_model=printer_model,
                        nozzle_diameter=nozzle_diameter,
                        supports_user_preset=supports_user_preset,
                    )
                    for note in plan.warnings:
                        logger.info("Spoolman link: %s", note)
                    # The K half below keys off the ACTUAL family, never the advertised one.
                    effective_tray_info_idx = plan.tray_info_idx

                    # Project under the printer's policy, publish, remember what was
                    # masked — the one helper all three assignment paths share. The
                    # actual plan stays the spool's truth; only what the printer is
                    # told changes.
                    printer_row = (await db.execute(select(Printer).where(Printer.id == p_id))).scalar_one_or_none()
                    # A slot the firmware calls empty will not echo the filament back, so it is
                    # published (this path has no replay on insert) but not verified; and a
                    # publish that went out asks for a fresh report, so the verification has
                    # current telemetry instead of an idle printer's next push (upstream 53ef17dc5).
                    slot_live_tray = live_tray_for(state, a_id, t_id)
                    sent, projection = await publish_projected_slot(
                        db,
                        mqtt_client,
                        printer=printer_row,
                        printer_id=p_id,
                        ams_id=a_id,
                        tray_id=t_id,
                        actual_plan=plan,
                        live_tray=slot_live_tray,
                        spool_tag_uid=mapped.get("tag_uid"),
                        spool_tray_uuid=mapped.get("tray_uuid"),
                        material=tray_type,
                        extra_colors=None,
                        printer_model=printer_model,
                        nozzle_diameter=nozzle_diameter,
                        supports_user_preset=supports_user_preset,
                        tray_sub_brands=tray_sub_brands,
                        tray_type_fallback=tray_type,
                        source="spoolman",
                        verify=not tray_definitely_empty(slot_live_tray),
                    )
                    if sent:
                        mqtt_client.request_status_update()

                    from backend.app.services.calibration_service import (  # noqa: PLC0415
                        apply_active_calibration_to_slot,
                    )

                    fired = False
                    if matching_fc and kprofile_allowed(projection):
                        fired, _ = await apply_active_calibration_to_slot(
                            db=db,
                            printer_id=p_id,
                            ams_id=a_id,
                            slot_id=t_id,
                            filament_id=matching_fc.filament_id,
                            nozzle_diameter=nozzle_dia_float,
                            nozzle_volume_type=matching_fc.nozzle_volume_type,
                            extruder_id=slot_extruder,
                        )
                    elif not matching_fc and effective_tray_info_idx and kprofile_allowed(projection):
                        # As the assign route: no link made by hand, so the spool
                        # gets its K by default through its filament — the ACTIVE
                        # calibration of the slot's combo, through the same helper
                        # the built-in inventory uses.
                        fired, _ = await apply_active_calibration_to_slot(
                            db=db,
                            printer_id=p_id,
                            ams_id=a_id,
                            slot_id=t_id,
                            filament_id=effective_tray_info_idx,
                            nozzle_diameter=nozzle_dia_float,
                            nozzle_volume_type=nozzle.flow_or_standard,
                            extruder_id=nozzle.extruder_or_default,
                        )
                    if not fired:
                        from backend.app.api.routes.inventory import _find_tray_in_ams_data  # noqa: PLC0415

                        live_tray = None
                        if state and state.raw_data:
                            ams_raw = state.raw_data.get("ams", [])
                            if isinstance(ams_raw, dict):
                                ams_raw = ams_raw.get("ams", [])
                            live_tray = _find_tray_in_ams_data(ams_raw, a_id, t_id)
                        live_cali_idx = (live_tray or {}).get("cali_idx")
                        # As the assign route: a family from the stored preset keeps
                        # the slot's index only when the printer's own table files
                        # that index under this filament.
                        from backend.app.utils.kprofile_lookup import slot_profile_filament_ids  # noqa: PLC0415

                        if plan.from_stored_preset and slot_profile_filament_ids(state, live_cali_idx, a_id, t_id) != {
                            effective_tray_info_idx
                        }:
                            live_cali_idx = None
                        if live_cali_idx is not None and live_cali_idx >= 0:
                            mqtt_client.extrusion_cali_sel(
                                ams_id=a_id,
                                tray_id=t_id,
                                cali_idx=live_cali_idx,
                                filament_id=effective_tray_info_idx,
                                nozzle_diameter=nozzle_diameter,
                            )

                    logger.info(
                        "Auto-configured AMS slot ams=%d tray=%d after linking Spoolman spool %d on printer %d",
                        a_id,
                        t_id,
                        spool_id,
                        p_id,
                    )
            except (SpoolmanNotFoundError, SpoolmanUnavailableError) as e:
                logger.warning(
                    "Could not fetch Spoolman spool %d for MQTT configure after tag link: %s",
                    spool_id,
                    e,
                )
            except Exception:
                logger.exception(
                    "Failed to auto-configure AMS slot after linking Spoolman spool %d (printer=%d ams=%d tray=%d)",
                    spool_id,
                    p_id,
                    a_id,
                    t_id,
                )

        # Sent last, like the assign route: everything a refetch reads — the tag,
        # the slot row, the stale tag links taken off other spools — is settled
        # (upstream 6c2731a6d). The tag → spool map hangs on ``inventory_changed``.
        await ws_manager.broadcast({"type": "inventory_changed"})
        if printer_context:
            await _announce_slot(*printer_context)

        return {"success": True, "message": f"Spool {spool_id} linked to AMS tag"}


async def _extra_tag_holders(client, tag: str, *, keep_spool_id: int) -> list[int]:
    """Spools other than ``keep_spool_id`` whose ``extra.tag`` is ``tag`` — a read before any mutation."""
    try:
        spools = await client.get_spools()
    except (SpoolmanClientError, SpoolmanUnavailableError):
        return []
    wanted = tag.upper()
    holders = []
    for spool in spools:
        spool_id = spool.get("id")
        if not isinstance(spool_id, int) or spool_id == keep_spool_id:
            continue
        raw = (spool.get("extra") or {}).get("tag", "")
        if isinstance(raw, str) and raw.strip('"').upper() == wanted:
            holders.append(spool_id)
    return holders


@router.post("/spools/{spool_id}/unlink")
async def unlink_spool(
    spool_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Unlink a Spoolman spool from AMS by clearing Spoolman extra.tag.

    One tag operation for all three steps — clearing ``extra.tag``, removing the
    native tags, deleting the local slot rows (C047 S32): a spool being deleted
    is 409 with nothing written.
    """
    with spoolman_binding_fence.tag_operation([spool_id], issued=spoolman_binding_fence.ticket()):
        return await _unlink_spool(spool_id, db)


async def _unlink_spool(spool_id: int, db: AsyncSession):
    sm = await get_spoolman_settings(db)
    enabled, url = sm["enabled"], sm["url"]
    if not enabled:
        raise HTTPException(status_code=400, detail="Spoolman integration is not enabled")

    client = await get_spoolman_client()
    if not client:
        if url:
            client = await init_spoolman_client(url)
        else:
            raise HTTPException(status_code=400, detail="Spoolman URL is not configured")

    if not await client.health_check():
        raise HTTPException(status_code=503, detail="Spoolman is not reachable")

    # Spoolman PATCHes the extra dict by MERGING with the existing keys —
    # popping "tag" from a copy of the dict and sending the rest doesn't
    # clear it; Spoolman keeps the old value because the key wasn't in the
    # payload. To actually clear a key we must explicitly send it as the
    # JSON-encoded empty string ('""'), which the read-side filters in
    # _map_spoolman_spool and get_linked_spools strip via .strip('"').
    #
    # merge_spool_extra acquires extra_lock(spool_id) internally — wrapping
    # this call in another `async with client.extra_lock(spool_id)` would
    # deadlock (asyncio.Lock is not reentrant).
    try:
        unlinked = await client.merge_spool_extra(spool_id, {"tag": json.dumps("")})
        # And its native tags, or the spool would still be found by each of them.
        await client.unlink_all_native_tags(unlinked)
    except SpoolmanNotFoundError:
        raise HTTPException(status_code=404, detail="Spool not found in Spoolman")
    except SpoolmanClientError:
        raise HTTPException(status_code=502, detail="Spoolman rejected the request")
    except SpoolmanUnavailableError:
        raise HTTPException(status_code=503, detail="Spoolman is not reachable")

    # Remove local slot assignment for this spool (all slots — a spool can only be in one at a time).
    # The route is addressed by spool, so the slots are knowable only from the
    # rows: read as plain values BEFORE the delete, as the unassign route does.
    try:
        freed_slots = [
            tuple(row)
            for row in (
                await db.execute(
                    select(
                        SpoolmanSlotAssignment.printer_id,
                        SpoolmanSlotAssignment.ams_id,
                        SpoolmanSlotAssignment.tray_id,
                    ).where(SpoolmanSlotAssignment.spoolman_spool_id == spool_id)
                )
            ).all()
        ]
        await db.execute(delete(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.spoolman_spool_id == spool_id))
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("DB error removing slot assignment for spool %s", spool_id)
        raise HTTPException(status_code=500, detail="Failed to remove local slot assignment")

    from backend.app.services import ams_advertised_overlay as overlay  # noqa: PLC0415

    await ws_manager.broadcast({"type": "inventory_changed"})
    for printer_id, ams_id, tray_id in freed_slots:
        # With the assignment gone there is no spool left behind the mask.
        overlay.forget(printer_id, ams_id, tray_id)
        await _announce_slot(printer_id, ams_id, tray_id)

    logger.info("Unlinked Spoolman spool %s", spool_id)
    return {"success": True, "message": f"Spool {spool_id} unlinked from AMS"}


class CreateSpoolFromSlotRequest(BaseModel):
    printer_id: int
    ams_id: int
    tray_id: int


@router.post("/spools/from-slot")
async def create_spool_from_slot(
    req: CreateSpoolFromSlotRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """One tag operation from ``sync_ams_tray`` to the UPSERT's commit (C047 S32, S37)."""
    with spoolman_binding_fence.tag_operation(issued=spoolman_binding_fence.ticket()):
        return await _create_spool_from_slot(req, db)


async def _create_spool_from_slot(req: CreateSpoolFromSlotRequest, db: AsyncSession):
    """Explicit user action: create a Spoolman spool from an AMS slot's current tray data.

    Used by the "+ Add to inventory" affordance when auto_add_unknown_rfid is disabled —
    the user looked at the slot and chose to register it. Calls sync_ams_tray with the
    auto-add override on so the spool is created even when the global setting is off.
    """
    sm = await get_spoolman_settings(db)
    if not sm["enabled"]:
        raise HTTPException(status_code=400, detail="Spoolman integration is not enabled")

    client = await get_spoolman_client()
    if not client:
        if sm["url"]:
            client = await init_spoolman_client(sm["url"])
        else:
            raise HTTPException(status_code=400, detail="Spoolman URL is not configured")

    if not await client.health_check():
        raise HTTPException(status_code=503, detail="Spoolman is not reachable")

    result = await db.execute(select(Printer).where(Printer.id == req.printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    state = printer_manager.get_status(req.printer_id)
    if not state or not state.raw_data:
        raise HTTPException(status_code=404, detail="Printer not connected or no state available")

    ams_data = state.raw_data.get("ams")
    ams_units: list[dict] = []
    if isinstance(ams_data, list):
        ams_units = ams_data
    elif isinstance(ams_data, dict):
        if "ams" in ams_data and isinstance(ams_data["ams"], list):
            ams_units = ams_data["ams"]
        elif "tray" in ams_data:
            ams_units = [{"id": 0, "tray": ams_data.get("tray", [])}]

    tray = None
    for unit in ams_units:
        if not isinstance(unit, dict):
            continue
        if int(unit.get("id", -1)) != req.ams_id:
            continue
        for t in unit.get("tray", []):
            if isinstance(t, dict) and int(t.get("id", -1)) == req.tray_id:
                tray = client.parse_ams_tray(req.ams_id, t)
                break
        if tray:
            break

    if not tray:
        raise HTTPException(status_code=400, detail="Slot is empty or has no readable tray data")

    # Same ghost-spool guard as the inventory route: no tag → no stable
    # identity → confirm would just create a fresh Spoolman row per push.
    from backend.app.services.spool_tag_matcher import is_valid_tag

    if not is_valid_tag(tray.tag_uid or "", tray.tray_uuid or ""):
        raise HTTPException(status_code=400, detail="Slot has no RFID tag")

    sync_result = await client.sync_ams_tray(
        tray,
        printer.name,
        disable_weight_sync=True,
        auto_add_unknown_rfid=True,
        db=db,
    )
    if not sync_result:
        # No spool was made. When the reel already belongs to a spool the lists
        # hide — an archived one, held by its chip alone — say whose it is.
        try:
            holder = await client.reel_holder(tray)
        except Exception:  # noqa: BLE001 — the reason is a courtesy; the refusal below stands without it
            holder = None
        if isinstance(holder, dict) and isinstance(holder.get("id"), int):
            raise native_tag_refused(
                "tray_uuid" if tray.tray_uuid and set(tray.tray_uuid) != {"0"} else "tag_uid",
                NativeTagConflict("", spool_id=holder["id"], archived=bool(holder.get("archived"))),
            )
        raise HTTPException(status_code=500, detail="Spoolman did not create a spool from the slot")

    # Persist the slot assignment so the new spool shows on the slot tile.
    # If this fails, surface a 500 — silently returning success while the
    # binding rolled back leaves the user thinking the spool was added,
    # then watching the modal re-fire on the next MQTT push.
    if sync_result.get("id"):
        # A binding writer (C047 §C3); the number is already owned by this operation.
        with spoolman_binding_fence.enter([sync_result["id"]], spoolman_binding_fence.ticket()):
            try:
                await db.execute(
                    text(
                        "INSERT INTO spoolman_slot_assignments"
                        " (printer_id, ams_id, tray_id, spoolman_spool_id)"
                        " VALUES (:printer_id, :ams_id, :tray_id, :spool_id)"
                        " ON CONFLICT(printer_id, ams_id, tray_id)"
                        " DO UPDATE SET spoolman_spool_id = excluded.spoolman_spool_id"
                    ),
                    {
                        "printer_id": req.printer_id,
                        "ams_id": req.ams_id,
                        "tray_id": req.tray_id,
                        "spool_id": sync_result["id"],
                    },
                )
                await db.commit()
            except Exception as exc:
                await db.rollback()
                logger.exception("Failed to persist Spoolman slot assignment")
                raise HTTPException(
                    status_code=500,
                    detail=f"Spool created in Spoolman but slot assignment failed: {exc}",
                ) from exc

    # The caller's own tab refreshes itself; every other one needs to be told.
    await ws_manager.broadcast({"type": "inventory_changed"})
    if sync_result.get("id"):
        await _announce_slot(req.printer_id, req.ams_id, req.tray_id)

    return {"success": True, "spool_id": sync_result.get("id")}
