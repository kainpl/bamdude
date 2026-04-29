"""Print scheduler service - processes the print queue."""

import asyncio
import json
import logging
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import defusedxml.ElementTree as ET
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.core.database import async_session
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.models.smart_plug import SmartPlug
from backend.app.services.notification_service import notification_service
from backend.app.services.printer_manager import (
    first_drying_blocking_reason,
    printer_manager,
    supports_drying,
)
from backend.app.services.smart_plug_manager import smart_plug_manager
from backend.app.utils.threemf_tools import extract_nozzle_mapping_from_3mf

logger = logging.getLogger(__name__)

# Filament type equivalence groups - types within the same group are
# interchangeable on the printer side (Bambu Lab firmware treats them as compatible).
_FILAMENT_TYPE_GROUPS: list[list[str]] = [
    ["PA-CF", "PA12-CF", "PAHT-CF"],
]
_FILAMENT_EQUIV_MAP: dict[str, str] = {}
for _group in _FILAMENT_TYPE_GROUPS:
    _canonical = _group[0].upper()
    for _t in _group:
        _FILAMENT_EQUIV_MAP[_t.upper()] = _canonical


def _canonical_filament_type(ftype: str) -> str:
    """Return canonical type for equivalence matching."""
    upper = ftype.upper()
    return _FILAMENT_EQUIV_MAP.get(upper, upper)


class _StaggerSlot:
    """Tracks a printer that recently started and is heating up."""

    __slots__ = ("printer_id", "started_at", "temp_reached_at", "interval_seconds")

    def __init__(self, printer_id: int, interval_seconds: int):
        self.printer_id = printer_id
        self.started_at = time.monotonic()
        self.temp_reached_at: float | None = None
        self.interval_seconds = interval_seconds  # per-printer or system default


class PrintScheduler:
    """Background scheduler that processes the print queue."""

    # Built-in drying presets per filament type (from BambuStudio filament profiles)
    # Format: { n3f_temp, n3s_temp, n3f_hours, n3s_hours }
    DEFAULT_DRYING_PRESETS: dict[str, dict[str, int]] = {
        "PLA": {"n3f": 45, "n3s": 45, "n3f_hours": 12, "n3s_hours": 12},
        "PETG": {"n3f": 65, "n3s": 65, "n3f_hours": 12, "n3s_hours": 12},
        "TPU": {"n3f": 65, "n3s": 75, "n3f_hours": 12, "n3s_hours": 18},
        "ABS": {"n3f": 65, "n3s": 80, "n3f_hours": 12, "n3s_hours": 8},
        "ASA": {"n3f": 65, "n3s": 80, "n3f_hours": 12, "n3s_hours": 8},
        "PA": {"n3f": 65, "n3s": 85, "n3f_hours": 12, "n3s_hours": 12},
        "PC": {"n3f": 65, "n3s": 80, "n3f_hours": 12, "n3s_hours": 8},
        "PVA": {"n3f": 65, "n3s": 85, "n3f_hours": 12, "n3s_hours": 18},
    }

    def __init__(self):
        self._running = False
        self._check_interval = 30  # seconds
        self._power_on_wait_time = 180  # seconds to wait for printer after power on (3 min)
        self._power_on_check_interval = 10  # seconds between connection checks
        self._min_drying_seconds = 1800  # 30 minutes minimum before humidity re-check can stop drying
        # Track which printers are currently auto-drying (printer_id -> start timestamp)
        self._drying_in_progress: dict[int, float] = {}
        # Staggered start: rolling slots for electrical load management
        self._stagger_slots: list[_StaggerSlot] = []
        # Defensive in-memory dispatch hold (#1157 ported from upstream v0.2.4b1):
        # a printer that just received a project_file command must not get a
        # second dispatch until either it transitions out of pre_state OR the
        # hard timeout expires. The H2D Pro can take 80–210 s to flip
        # FINISH→PREPARE after project_file, and during that window the
        # PrinterQueue.status='printing' DB seed is empirically unreliable on
        # multi-plate batches (same-file plates double-/triple-dispatched onto
        # the same printer 30 s apart). Keyed in-memory per printer; cleared by
        # the watchdog on success or revert. Pure additive — sits alongside
        # _is_printer_idle() and the DB seed.
        # printer_id -> (monotonic_started_at, pre_state, pre_subtask_id)
        self._dispatch_holds: dict[int, tuple[float, str, str | None]] = {}
        # Minimum cooldown between dispatches to the same printer (covers the
        # H2D's project_file digestion window).
        self._dispatch_min_cooldown = 60.0
        # Hard timeout — drop the hold even if we never observed a transition,
        # so a lost MQTT session can't lock a printer out of the queue forever.
        # Matches the watchdog timeout (90 s) plus a safety margin so the
        # watchdog's release runs first on the unhappy path.
        self._dispatch_max_hold = 180.0

    async def run(self):
        """Main loop - check queue every interval."""
        self._running = True
        logger.info("Print scheduler started")

        while self._running:
            try:
                await self.check_queue()
            except Exception as e:
                logger.error("Scheduler error: %s", e)

            await asyncio.sleep(self._check_interval)

    def stop(self):
        """Stop the scheduler."""
        self._running = False
        logger.info("Print scheduler stopped")

    async def check_queue(self):
        """Check for prints ready to start."""
        async with async_session() as db:
            # Get all pending items with queue loaded, ordered by queue and position
            from sqlalchemy.orm import selectinload

            result = await db.execute(
                select(PrintQueueItem)
                .options(selectinload(PrintQueueItem.queue))
                .where(PrintQueueItem.status == "pending")
                .order_by(PrintQueueItem.queue_id, PrintQueueItem.position)
            )
            items = list(result.scalars().all())

            if not items:
                # No pending items - still check auto-drying on idle printers
                await self._check_auto_drying(db, [], set())
                return

            logger.info(
                "Queue check: found %d pending items: %s",
                len(items),
                [(i.id, i.queue_id, i.archive_id, i.library_file_id) for i in items],
            )

            # Seed busy_printers from PrinterQueue.status='printing'. This is
            # the authoritative "this printer is currently dispatched" marker —
            # set_queue_busy() atomically flips queue.status='printing' at
            # dispatch time (whether item, external, or direct-print). Without
            # this guard the H2D / P1 IDLE→RUNNING MQTT transition lag made the
            # next check_queue tick see IDLE via _is_printer_idle() and double-
            # dispatch onto the already-running printer (upstream #950286ad /
            # v0.2.3.2 re-release). Queue-per-printer architecture — reading
            # PrinterQueue directly is simpler than joining through
            # PrintQueueItem and also catches external / direct prints that
            # don't have a corresponding item row.
            from backend.app.models.print_queue import PrinterQueue

            busy_result = await db.execute(
                select(PrinterQueue.printer_id)
                .where(PrinterQueue.status == "printing")
                .where(PrinterQueue.printer_id.is_not(None))
            )
            busy_printers: set[int] = {pid for (pid,) in busy_result.all() if pid is not None}

            # Defense-in-depth (#1157): augment busy_printers with any printer
            # still inside its post-dispatch hold window. The DB seed above can
            # miss in-flight items in a multi-plate batch — same-file plates
            # were being dispatched 30 s apart while the H2D was still digesting
            # the first project_file. The hold is keyed in-memory and released
            # by the watchdog on the success path, so it adds a layer that
            # doesn't depend on DB row visibility or completion-callback timing.
            for held_printer_id in list(self._dispatch_holds.keys()):
                if self._printer_in_dispatch_hold(held_printer_id):
                    busy_printers.add(held_printer_id)

            # Cache per-printer require_plate_clear setting
            _plate_clear_cache: dict[int, bool] = {}

            async def _get_require_plate_clear(pid: int) -> bool:
                if pid not in _plate_clear_cache:
                    from backend.app.models.printer import Printer

                    result = await db.execute(select(Printer.require_plate_clear).where(Printer.id == pid))
                    val = result.scalar_one_or_none()
                    _plate_clear_cache[pid] = val if val is not None else True
                return _plate_clear_cache[pid]

            # Staggered start: update temps and clean expired slots
            stagger_enabled, stagger_concurrent, stagger_interval, stagger_wait_bed = await self._get_stagger_settings(
                db
            )
            if stagger_enabled:
                self._update_stagger_temps()
                self._cleanup_stagger_slots(stagger_wait_bed)

            # Log skip reasons once per queue check (not per item)
            skip_reasons: dict[str, int] = {}

            for item in items:
                # Skip items in paused or error queues
                if item.queue and item.queue.status in ("paused", "error"):
                    skip_reasons[f"queue_{item.queue.status}"] = skip_reasons.get(f"queue_{item.queue.status}", 0) + 1
                    continue

                # Get printer_id from queue
                printer_id = item.queue.printer_id if item.queue else None
                if not printer_id:
                    continue

                # Check scheduled time first (scheduled_time is stored in UTC from ISO string)
                if item.scheduled_time:
                    sched = item.scheduled_time
                    if sched.tzinfo is None:
                        sched = sched.replace(tzinfo=timezone.utc)
                    if sched > datetime.now(timezone.utc):
                        skip_reasons["scheduled_future"] = skip_reasons.get("scheduled_future", 0) + 1
                        continue

                # Skip items that require manual start
                if item.manual_start:
                    skip_reasons["manual_start"] = skip_reasons.get("manual_start", 0) + 1
                    continue

                # Skip if printer already busy this iteration
                if printer_id in busy_printers:
                    continue

                # Check if printer is idle
                rpc = await _get_require_plate_clear(printer_id)
                printer_idle = self._is_printer_idle(printer_id, require_plate_clear=rpc)
                printer_connected = printer_manager.is_connected(printer_id)

                # Update waiting_reason based on current state
                new_reason = None
                if not printer_connected:
                    new_reason = "Printer offline"
                elif not printer_idle:
                    if self._drying_in_progress.get(printer_id):
                        new_reason = "Drying in progress"
                    elif rpc and printer_manager.is_awaiting_plate_clear(printer_id):
                        status = printer_manager.get_status(printer_id)
                        if status and status.state in ("FINISH", "FAILED"):
                            new_reason = "Plate not cleared"

                if item.waiting_reason != new_reason:
                    item.waiting_reason = new_reason
                    await db.commit()

                # If printer not connected, try to power on via smart plug(s)
                if not printer_connected:
                    plugs = await self._get_smart_plugs(db, printer_id)
                    auto_on_plugs = [p for p in plugs if p.auto_on and p.enabled]
                    if auto_on_plugs:
                        logger.info("Printer %s offline, attempting to power on via smart plug(s)", printer_id)
                        # Power on primary plug and wait for printer to connect
                        powered_on = await self._power_on_and_wait(auto_on_plugs[0], printer_id, db)
                        if powered_on:
                            # Also turn on remaining auto_on plugs (filter, secondary power, etc.)
                            for extra_plug in auto_on_plugs[1:]:
                                try:
                                    service = await smart_plug_manager.get_service_for_plug(extra_plug, db)
                                    await service.turn_on(extra_plug)
                                    logger.info("Also powered on plug '%s' for printer %s", extra_plug.name, printer_id)
                                except Exception as e:
                                    logger.warning("Failed to power on extra plug '%s': %s", extra_plug.name, e)
                            printer_connected = True
                            printer_idle = self._is_printer_idle(printer_id, require_plate_clear=rpc)
                        else:
                            logger.warning("Could not power on printer %s via smart plug", printer_id)
                            busy_printers.add(printer_id)
                            continue
                    else:
                        busy_printers.add(printer_id)
                        continue

                # Check if printer is idle (busy with another print)
                if not printer_idle:
                    if self._drying_in_progress.get(printer_id):
                        block_for_drying = await self._get_bool_setting(db, "queue_drying_block")
                        if block_for_drying:
                            busy_printers.add(printer_id)
                            continue
                        else:
                            await self._stop_drying(printer_id)
                            printer_idle = self._is_printer_idle(printer_id, require_plate_clear=rpc)
                            if not printer_idle:
                                busy_printers.add(printer_id)
                                continue
                    else:
                        busy_printers.add(printer_id)
                        continue

                # Staggered start: check if we have a free slot
                if stagger_enabled and not self._can_start_staggered(stagger_concurrent):
                    stagger_reason = self._stagger_reason(stagger_wait_bed)
                    if item.waiting_reason != stagger_reason:
                        item.waiting_reason = stagger_reason
                        await db.commit()
                    skip_reasons["stagger_wait"] = skip_reasons.get("stagger_wait", 0) + 1
                    continue

                # Clear waiting_reason - printer is ready
                if item.waiting_reason:
                    item.waiting_reason = None
                    await db.commit()

                # Compute AMS mapping if not already set
                if not item.ams_mapping:
                    computed_mapping = await self._compute_ams_mapping_for_printer(db, printer_id, item)
                    if computed_mapping:
                        item.ams_mapping = json.dumps(computed_mapping)
                        logger.info(
                            f"Queue item {item.id}: Computed AMS mapping for printer {printer_id}: {computed_mapping}"
                        )
                        await db.commit()

                # Start the print
                await self._start_print(db, item)
                busy_printers.add(printer_id)

                # Register stagger slot after successful start
                if stagger_enabled:
                    # Per-printer interval override (0 = use system default)
                    printer_obj = await self._get_printer(db, printer_id)
                    per_printer_iv = (
                        (printer_obj.stagger_interval_minutes * 60)
                        if printer_obj and printer_obj.stagger_interval_minutes
                        else 0
                    )
                    self._register_stagger_start(printer_id, per_printer_iv or stagger_interval)

            # Log summary of skip reasons (helps diagnose why queue items aren't starting)
            if skip_reasons:
                logger.info("Queue skip summary: %s", skip_reasons)
            if busy_printers:
                # Log why each printer was busy (first time it was checked)
                for pid in busy_printers:
                    state = printer_manager.get_status(pid)
                    connected = printer_manager.is_connected(pid)
                    awaiting_plate_clear = printer_manager.is_awaiting_plate_clear(pid)
                    state_name = state.state if state else "NO_STATUS"
                    logger.info(
                        "Queue: printer %d not available - connected=%s, state=%s, awaiting_plate_clear=%s",
                        pid,
                        connected,
                        state_name,
                        awaiting_plate_clear,
                    )

            # Auto-drying: start drying on idle printers that have no pending queue items
            await self._check_auto_drying(db, items, busy_printers)

    async def _compute_ams_mapping_for_printer(
        self, db: AsyncSession, printer_id: int, item: PrintQueueItem
    ) -> list[int] | None:
        """Compute AMS mapping for a printer based on filament requirements.

        Called when a queue item has no ams_mapping set - either for model-based
        items after printer assignment, or printer-specific items (e.g. from VP).

        Args:
            db: Database session
            printer_id: The assigned printer ID
            item: The queue item (contains archive_id or library_file_id)

        Returns:
            AMS mapping array or None if no mapping needed/possible
        """
        # Get printer status
        status = printer_manager.get_status(printer_id)
        if not status:
            logger.warning("Cannot compute AMS mapping: printer %s status unavailable", printer_id)
            return None

        # Get filament requirements from source file
        filament_reqs = await self._get_filament_requirements(db, item)
        if not filament_reqs:
            logger.debug("No filament requirements found for queue item %s", item.id)
            return None

        # Apply filament overrides if present
        if item.filament_overrides:
            try:
                overrides = json.loads(item.filament_overrides)
                override_map = {o["slot_id"]: o for o in overrides}
                for req in filament_reqs:
                    if req["slot_id"] in override_map:
                        override = override_map[req["slot_id"]]
                        req["type"] = override["type"]
                        req["color"] = override["color"]
                        # Clear tray_info_idx so matching uses type+color instead of
                        # the original 3MF's tray_info_idx (which would match the old filament)
                        req["tray_info_idx"] = ""
                        logger.debug(
                            "Queue item %s: Override slot %d -> %s %s",
                            item.id,
                            req["slot_id"],
                            override["type"],
                            override["color"],
                        )
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning("Failed to apply filament overrides for queue item %s: %s", item.id, e)

        # Build loaded filaments from printer status
        loaded_filaments = self._build_loaded_filaments(status)
        if not loaded_filaments:
            logger.debug("No filaments loaded on printer %s", printer_id)
            return None

        # Check if user prefers lowest remaining filament when multiple spools match
        prefer_lowest = await self._get_bool_setting(db, "prefer_lowest_filament")

        # Compute mapping: match required filaments to available slots
        return self._match_filaments_to_slots(filament_reqs, loaded_filaments, prefer_lowest)

    async def _get_filament_requirements(self, db: AsyncSession, item: PrintQueueItem) -> list[dict] | None:
        """Extract filament requirements from the source 3MF file.

        Args:
            db: Database session
            item: Queue item with archive_id or library_file_id

        Returns:
            List of filament requirement dicts with slot_id, type, color, used_grams
        """
        file_path: Path | None = None

        if item.archive_id:
            result = await db.execute(select(PrintArchive).where(PrintArchive.id == item.archive_id))
            archive = result.scalar_one_or_none()
            if archive:
                file_path = settings.base_dir / archive.file_path
        elif item.library_file_id:
            result = await db.execute(select(LibraryFile).where(LibraryFile.id == item.library_file_id))
            library_file = result.scalar_one_or_none()
            if library_file:
                lib_path = Path(library_file.file_path)
                file_path = lib_path if lib_path.is_absolute() else settings.base_dir / library_file.file_path

        if not file_path or not file_path.exists():
            return None

        filaments = []
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                if "Metadata/slice_info.config" not in zf.namelist():
                    return None

                content = zf.read("Metadata/slice_info.config").decode()
                root = ET.fromstring(content)

                # Check if plate_id is specified - use that plate's filaments
                plate_id = item.plate_id
                if plate_id:
                    for plate_elem in root.findall("./plate"):
                        plate_index = None
                        for meta in plate_elem.findall("metadata"):
                            if meta.get("key") == "index":
                                plate_index = int(meta.get("value", "0"))
                                break
                        if plate_index == plate_id:
                            for filament_elem in plate_elem.findall("./filament"):
                                filament_id = filament_elem.get("id")
                                filament_type = filament_elem.get("type", "")
                                filament_color = filament_elem.get("color", "")
                                # tray_info_idx identifies the specific spool selected when slicing
                                tray_info_idx = filament_elem.get("tray_info_idx", "")
                                used_g = filament_elem.get("used_g", "0")
                                try:
                                    used_grams = float(used_g)
                                    if used_grams > 0 and filament_id:
                                        filaments.append(
                                            {
                                                "slot_id": int(filament_id),
                                                "type": filament_type,
                                                "color": filament_color,
                                                "tray_info_idx": tray_info_idx,
                                                "used_grams": round(used_grams, 1),
                                            }
                                        )
                                except (ValueError, TypeError):
                                    pass  # Skip filament entry with unparseable usage data
                            break
                else:
                    # No plate_id - extract all filaments with used_g > 0
                    for filament_elem in root.findall("./filament"):
                        filament_id = filament_elem.get("id")
                        filament_type = filament_elem.get("type", "")
                        filament_color = filament_elem.get("color", "")
                        # tray_info_idx identifies the specific spool selected when slicing
                        tray_info_idx = filament_elem.get("tray_info_idx", "")
                        used_g = filament_elem.get("used_g", "0")
                        try:
                            used_grams = float(used_g)
                            if used_grams > 0 and filament_id:
                                filaments.append(
                                    {
                                        "slot_id": int(filament_id),
                                        "type": filament_type,
                                        "color": filament_color,
                                        "tray_info_idx": tray_info_idx,
                                        "used_grams": round(used_grams, 1),
                                    }
                                )
                        except (ValueError, TypeError):
                            pass  # Skip filament entry with unparseable usage data

                filaments.sort(key=lambda x: x["slot_id"])

                # Enrich with nozzle mapping for dual-nozzle printers
                nozzle_mapping = extract_nozzle_mapping_from_3mf(zf)
                if nozzle_mapping:
                    for filament in filaments:
                        filament["nozzle_id"] = nozzle_mapping.get(filament["slot_id"])
        except Exception as e:
            logger.warning("Failed to parse filament requirements: %s", e)
            return None

        return filaments if filaments else None

    def _build_loaded_filaments(self, status) -> list[dict]:
        """Build list of loaded filaments from printer status.

        Args:
            status: PrinterState from printer_manager

        Returns:
            List of loaded filament dicts with type, color, ams_id, tray_id, global_tray_id
        """
        filaments = []

        # Get ams_extruder_map for dual-nozzle printers (H2D, H2D Pro)
        ams_extruder_map = status.raw_data.get("ams_extruder_map", {})

        # Parse AMS units from raw_data
        ams_data = status.raw_data.get("ams", [])
        for ams_unit in ams_data:
            ams_id = int(ams_unit.get("id", 0))
            trays = ams_unit.get("tray", [])
            is_ht = len(trays) == 1  # AMS-HT has single tray

            for tray in trays:
                tray_type = tray.get("tray_type")
                if tray_type:
                    tray_id = int(tray.get("id", 0))
                    tray_color = tray.get("tray_color", "")
                    # tray_info_idx identifies the specific spool (e.g., "GFA00", "P4d64437")
                    tray_info_idx = tray.get("tray_info_idx", "")
                    # Normalize color: remove alpha, add hash
                    color = self._normalize_color(tray_color)
                    # Calculate global tray ID
                    # AMS-HT units have IDs starting at 128 with a single tray
                    global_tray_id = ams_id if ams_id >= 128 else ams_id * 4 + tray_id

                    filaments.append(
                        {
                            "type": tray_type,
                            "color": color,
                            "tray_info_idx": tray_info_idx,
                            "ams_id": ams_id,
                            "tray_id": tray_id,
                            "is_ht": is_ht,
                            "is_external": False,
                            "global_tray_id": global_tray_id,
                            "extruder_id": ams_extruder_map.get(str(ams_id)),
                            "remain": tray.get("remain", -1),
                        }
                    )

        # Check external spool(s) (vt_tray is a list)
        for idx, vt in enumerate(status.raw_data.get("vt_tray") or []):
            if vt.get("tray_type"):
                color = self._normalize_color(vt.get("tray_color", ""))
                tray_id = int(vt.get("id", 254))
                filaments.append(
                    {
                        "type": vt["tray_type"],
                        "color": color,
                        "tray_info_idx": vt.get("tray_info_idx", ""),
                        "ams_id": -1,
                        "tray_id": idx,
                        "is_ht": False,
                        "is_external": True,
                        "global_tray_id": tray_id,
                        "extruder_id": (255 - tray_id) if ams_extruder_map else None,
                        "remain": vt.get("remain", -1),
                    }
                )

        return filaments

    def _normalize_color(self, color: str | None) -> str:
        """Normalize color to #RRGGBB format."""
        if not color:
            return "#808080"
        hex_color = color.replace("#", "")[:6]
        return f"#{hex_color}"

    def _normalize_color_for_compare(self, color: str | None) -> str:
        """Normalize color for comparison (lowercase, no hash)."""
        if not color:
            return ""
        return color.replace("#", "").lower()[:6]

    def _colors_are_similar(self, color1: str | None, color2: str | None, threshold: int = 40) -> bool:
        """Check if two colors are visually similar within a threshold."""
        hex1 = self._normalize_color_for_compare(color1)
        hex2 = self._normalize_color_for_compare(color2)
        if not hex1 or not hex2 or len(hex1) < 6 or len(hex2) < 6:
            return False

        try:
            r1 = int(hex1[0:2], 16)
            g1 = int(hex1[2:4], 16)
            b1 = int(hex1[4:6], 16)
            r2 = int(hex2[0:2], 16)
            g2 = int(hex2[2:4], 16)
            b2 = int(hex2[4:6], 16)
            return abs(r1 - r2) <= threshold and abs(g1 - g2) <= threshold and abs(b1 - b2) <= threshold
        except ValueError:
            return False

    def _match_filaments_to_slots(
        self, required: list[dict], loaded: list[dict], prefer_lowest: bool = False
    ) -> list[int] | None:
        """Match required filaments to loaded filaments and build AMS mapping.

        Priority: unique tray_info_idx match > exact color match > similar color match > type-only match

        The tray_info_idx is a filament type identifier stored in the 3MF file when the user
        slices (e.g., "GFA00" for generic PLA, "P4d64437" for custom presets). If the same
        tray_info_idx appears in only ONE available tray, we use that tray. If multiple trays
        have the same tray_info_idx (e.g., two spools of generic PLA), we fall back to color
        matching among those trays.

        Args:
            required: List of required filaments with slot_id, type, color, tray_info_idx
            loaded: List of loaded filaments with type, color, tray_info_idx, global_tray_id

        Returns:
            AMS mapping array (position = slot_id - 1, value = global_tray_id or -1)
        """
        if not required:
            return None

        # Track used trays to avoid duplicate assignment
        used_tray_ids: set[int] = set()
        comparisons = []

        for req in required:
            req_type = (req.get("type") or "").upper()
            req_color = req.get("color", "")
            req_tray_info_idx = req.get("tray_info_idx", "")

            # Find best match: unique tray_info_idx > exact color > similar color > type-only
            idx_match = None
            exact_match = None
            similar_match = None
            type_only_match = None

            # Get available trays (not already used)
            available = [f for f in loaded if f["global_tray_id"] not in used_tray_ids]

            # Nozzle-aware filtering: restrict to trays on the correct nozzle.
            # Hard filter - cross-nozzle assignment causes print failures
            # ("position of left hotend is abnormal"), so never fall back.
            req_nozzle_id = req.get("nozzle_id")
            if req_nozzle_id is not None:
                available = [f for f in available if f.get("extruder_id") == req_nozzle_id]

            # Sort by remaining filament (ascending) so lowest-remain spool wins
            if prefer_lowest:
                available.sort(key=lambda f: f.get("remain", -1) if f.get("remain", -1) >= 0 else 101)

            # Check if tray_info_idx is unique among available trays
            if req_tray_info_idx:
                idx_matches = [f for f in available if f.get("tray_info_idx") == req_tray_info_idx]
                if len(idx_matches) == 1:
                    # Unique tray_info_idx - use it as definitive match
                    idx_match = idx_matches[0]
                    logger.debug(
                        f"Matched filament slot {req.get('slot_id')} by unique tray_info_idx={req_tray_info_idx} "
                        f"-> tray {idx_match['global_tray_id']}"
                    )
                elif len(idx_matches) > 1:
                    # Multiple trays with same tray_info_idx - use color matching among them
                    logger.debug(
                        f"Non-unique tray_info_idx={req_tray_info_idx} found in {len(idx_matches)} trays, "
                        f"using color matching among trays: {[f['global_tray_id'] for f in idx_matches]}"
                    )
                    # Use color matching within this subset
                    for f in idx_matches:
                        f_color = f.get("color", "")
                        if self._normalize_color_for_compare(f_color) == self._normalize_color_for_compare(req_color):
                            if not exact_match:
                                exact_match = f
                        elif self._colors_are_similar(f_color, req_color):
                            if not similar_match:
                                similar_match = f
                        elif not type_only_match:
                            type_only_match = f

            # If no idx_match yet, do standard type/color matching on all available trays
            if not idx_match and not exact_match and not similar_match and not type_only_match:
                for f in available:
                    f_type = (f.get("type") or "").upper()
                    if _canonical_filament_type(f_type) != _canonical_filament_type(req_type):
                        continue

                    # Type matches - check color
                    f_color = f.get("color", "")
                    if self._normalize_color_for_compare(f_color) == self._normalize_color_for_compare(req_color):
                        if not exact_match:
                            exact_match = f
                    elif self._colors_are_similar(f_color, req_color):
                        if not similar_match:
                            similar_match = f
                    elif not type_only_match:
                        type_only_match = f

            match = idx_match or exact_match or similar_match or type_only_match
            if match:
                used_tray_ids.add(match["global_tray_id"])
                comparisons.append({"slot_id": req.get("slot_id", 0), "global_tray_id": match["global_tray_id"]})
            else:
                comparisons.append({"slot_id": req.get("slot_id", 0), "global_tray_id": -1})

        # Build mapping array
        if not comparisons:
            return None

        max_slot_id = max(c["slot_id"] for c in comparisons)
        if max_slot_id <= 0:
            return None

        mapping = [-1] * max_slot_id
        for c in comparisons:
            slot_id = c["slot_id"]
            if slot_id and slot_id > 0:
                mapping[slot_id - 1] = c["global_tray_id"]

        return mapping

    # ── Staggered start helpers ──────────────────────────────────────────

    async def get_stagger_state_snapshot(self, db: AsyncSession) -> dict:
        """Return current stagger state for UI diagnostics.

        Shape:
            {
              "enabled": bool,
              "concurrent": int,
              "interval_minutes": int,
              "wait_for_bed": bool,
              "slots": [
                {"printer_id": int, "printer_name": str,
                 "started_at": float, "temp_reached_at": float | None,
                 "state": "heating" | "interval_wait",
                 "seconds_to_free": int},
                ...
              ],
              "free_slots": int,
              "next_free_in_seconds": int | None,
            }
        """
        enabled, concurrent, interval_seconds, wait_for_bed = await self._get_stagger_settings(db)
        now = time.monotonic()

        slots: list[dict] = []
        times_to_free: list[int] = []
        for slot in self._stagger_slots:
            info = printer_manager.get_printer(slot.printer_id)
            name = info.name if info else f"Printer #{slot.printer_id}"

            if wait_for_bed:
                if slot.temp_reached_at is None:
                    slot_state = "heating"
                    seconds_to_free = max(0, int(slot.interval_seconds))
                else:
                    slot_state = "interval_wait"
                    seconds_to_free = max(0, int(slot.interval_seconds - (now - slot.temp_reached_at)))
            else:
                slot_state = "interval_wait"
                seconds_to_free = max(0, int(slot.interval_seconds - (now - slot.started_at)))

            slots.append(
                {
                    "printer_id": slot.printer_id,
                    "printer_name": name,
                    "started_at": slot.started_at,
                    "temp_reached_at": slot.temp_reached_at,
                    "state": slot_state,
                    "seconds_to_free": seconds_to_free,
                    "interval_seconds": slot.interval_seconds,
                }
            )
            times_to_free.append(seconds_to_free)

        free_slots = max(0, concurrent - len(self._stagger_slots))
        next_free = None if free_slots > 0 or not times_to_free else min(times_to_free)

        return {
            "enabled": enabled,
            "concurrent": concurrent,
            "interval_minutes": interval_seconds // 60,
            "wait_for_bed": wait_for_bed,
            "slots": slots,
            "free_slots": free_slots,
            "next_free_in_seconds": next_free,
        }

    async def _get_stagger_settings(self, db: AsyncSession) -> tuple[bool, int, int, bool]:
        """Return (enabled, concurrent, interval_seconds, wait_for_bed)."""
        enabled = await self._get_bool_setting(db, "stagger_enabled")
        if not enabled:
            return False, 0, 0, False
        concurrent = int((await self._get_setting_value(db, "stagger_concurrent")) or "2")
        interval_min = int((await self._get_setting_value(db, "stagger_interval_minutes")) or "5")
        wait_for_bed = await self._get_bool_setting(db, "stagger_wait_for_bed")
        return True, max(concurrent, 1), interval_min * 60, wait_for_bed

    def _update_stagger_temps(self) -> None:
        """Check bed temps for printers in stagger slots, mark reached."""
        for slot in self._stagger_slots:
            if slot.temp_reached_at is not None:
                continue
            state = printer_manager.get_status(slot.printer_id)
            if not state:
                continue
            bed = state.temperatures.get("bed", 0)
            target = state.temperatures.get("bed_target", 0)
            if target > 0 and abs(bed - target) <= 1.0:
                slot.temp_reached_at = time.monotonic()
                logger.info(
                    "Stagger: printer %d bed reached %.1f°C (target %.1f°C), slot freed",
                    slot.printer_id,
                    bed,
                    target,
                )

    def _cleanup_stagger_slots(self, wait_for_bed: bool) -> None:
        """Remove slots that are fully expired (temp reached + interval elapsed)."""
        now = time.monotonic()
        active = []
        for slot in self._stagger_slots:
            iv = slot.interval_seconds

            # Check if the printer is still printing - if not, slot is done
            state = printer_manager.get_status(slot.printer_id)
            if state and state.state not in ("RUNNING", "PREPARE", "IDLE", "PAUSE"):
                # Printer finished/failed/offline - don't hold the slot
                if slot.temp_reached_at is not None:
                    if now - slot.temp_reached_at < iv:
                        active.append(slot)
                    continue
                continue

            if wait_for_bed:
                if slot.temp_reached_at is None:
                    active.append(slot)  # still heating
                elif now - slot.temp_reached_at < iv:
                    active.append(slot)  # interval not elapsed
            else:
                if now - slot.started_at < iv:
                    active.append(slot)  # interval not elapsed
        self._stagger_slots = active

    def _can_start_staggered(self, concurrent: int) -> bool:
        """Check if there's a free stagger slot."""
        return len(self._stagger_slots) < concurrent

    def _register_stagger_start(self, printer_id: int, interval_seconds: int) -> None:
        """Register a printer as occupying a stagger slot."""
        # Remove any old slot for same printer
        self._stagger_slots = [s for s in self._stagger_slots if s.printer_id != printer_id]
        self._stagger_slots.append(_StaggerSlot(printer_id, interval_seconds))
        logger.info(
            "Stagger: printer %d started (interval=%ds), %d slots occupied",
            printer_id,
            interval_seconds,
            len(self._stagger_slots),
        )

    def _stagger_reason(self, wait_for_bed: bool) -> str:
        """Get waiting reason for stagger-blocked items."""
        if wait_for_bed:
            heating = []
            for s in self._stagger_slots:
                if s.temp_reached_at is None:
                    info = printer_manager.get_printer(s.printer_id)
                    name = info.name if info else f"#{s.printer_id}"
                    heating.append(name)
            if heating:
                return f"Staggered start: waiting for {', '.join(heating)} to heat up"
        return "Staggered start: waiting for interval"

    def _mark_printer_dispatched(
        self,
        printer_id: int,
        pre_state: str | None,
        pre_subtask_id: str | None,
    ) -> None:
        """Record that a print command was just sent to ``printer_id``.

        Held until either the watchdog observes a state/subtask transition
        (success path) or the hard timeout expires. See ``_dispatch_holds``.
        """
        if not pre_state:
            # No pre_state means we can't detect a transition — fall back to a
            # pure time-based hold using empty string as a sentinel that won't
            # match any real printer state.
            pre_state = ""
        self._dispatch_holds[printer_id] = (time.monotonic(), pre_state, pre_subtask_id)

    def _release_dispatch_hold(self, printer_id: int) -> None:
        """Drop the dispatch hold for ``printer_id`` (called by the watchdog)."""
        self._dispatch_holds.pop(printer_id, None)

    def _printer_in_dispatch_hold(self, printer_id: int) -> bool:
        """True if ``printer_id`` is still inside its post-dispatch hold window.

        Returns False (and clears the hold) once any of these are true:
          - hard timeout (``_dispatch_max_hold``) has elapsed
          - the printer has transitioned out of pre_state and we're past the
            minimum cooldown
          - the printer's subtask_id has advanced past pre_subtask_id and we're
            past the minimum cooldown
        Otherwise the printer is held — caller should treat it as busy.
        """
        entry = self._dispatch_holds.get(printer_id)
        if not entry:
            return False
        started_at, pre_state, pre_subtask_id = entry
        elapsed = time.monotonic() - started_at

        if elapsed >= self._dispatch_max_hold:
            self._dispatch_holds.pop(printer_id, None)
            return False

        # Without a pre_state we can't detect a transition — fall back to the
        # min cooldown alone, then drop the hold.
        if not pre_state:
            if elapsed >= self._dispatch_min_cooldown:
                self._dispatch_holds.pop(printer_id, None)
                return False
            return True

        status = printer_manager.get_status(printer_id)
        current_state = getattr(status, "state", None) if status else None
        current_subtask_id = getattr(status, "subtask_id", None) if status else None
        transitioned = (current_state is not None and current_state != pre_state) or (
            pre_subtask_id is not None and current_subtask_id is not None and current_subtask_id != pre_subtask_id
        )

        if transitioned and elapsed >= self._dispatch_min_cooldown:
            self._dispatch_holds.pop(printer_id, None)
            return False

        return True

    def _is_printer_idle(self, printer_id: int, require_plate_clear: bool = True) -> bool:
        """Check if a printer is connected and idle."""
        if not printer_manager.is_connected(printer_id):
            logger.debug("Printer %d: not connected", printer_id)
            return False

        state = printer_manager.get_status(printer_id)
        if not state:
            logger.debug("Printer %d: no status available", printer_id)
            return False

        # IDLE = ready for next print
        # FINISH/FAILED = ready if plate-clear not required, or user confirmed plate is cleared
        # Printer is ready for dispatch when it's IDLE (never printed / user cleared)
        # OR at FINISH/FAILED with the plate-clear gate released. The gate is the
        # persisted ``awaiting_plate_clear`` flag inverted — absent means clear,
        # present means still waiting on user confirmation.
        idle = state.state == "IDLE" or (
            state.state in ("FINISH", "FAILED")
            and (not require_plate_clear or not printer_manager.is_awaiting_plate_clear(printer_id))
        )
        if not idle:
            logger.debug(
                "Printer %d: not idle - state=%s, awaiting_plate_clear=%s",
                printer_id,
                state.state,
                printer_manager.is_awaiting_plate_clear(printer_id),
            )
        return idle

    async def _get_setting_value(self, db: AsyncSession, key: str) -> str | None:
        """Read a raw setting value from the database."""
        result = await db.execute(select(Settings).where(Settings.key == key))
        setting = result.scalar_one_or_none()
        return setting.value if setting else None

    async def _get_bool_setting(self, db: AsyncSession, key: str, default: bool = False) -> bool:
        """Read a boolean setting from the database."""
        val = await self._get_setting_value(db, key)
        if val is not None:
            return val.lower() == "true"
        return default

    async def _get_drying_presets(self, db: AsyncSession) -> dict[str, dict[str, int]]:
        """Get drying presets (user-configured or built-in defaults)."""
        result = await db.execute(select(Settings).where(Settings.key == "drying_presets"))
        setting = result.scalar_one_or_none()
        if setting and setting.value:
            try:
                presets = json.loads(setting.value)
                if isinstance(presets, dict) and presets:
                    return presets
            except json.JSONDecodeError:
                pass
        return self.DEFAULT_DRYING_PRESETS

    def _get_conservative_drying_params(
        self, trays: list[dict], module_type: str, presets: dict[str, dict[str, int]]
    ) -> tuple[int, int, str] | None:
        """Get the most conservative drying params for mixed filament types in an AMS unit.

        Returns (temp, duration_hours, filament_type) or None if no drying-eligible filaments.
        """
        temp_key = module_type if module_type in ("n3f", "n3s") else "n3f"
        hours_key = f"{temp_key}_hours"

        min_temp = None
        max_hours = None
        filament_type = ""

        for tray in trays:
            tray_type = tray.get("tray_type", "")
            if not tray_type:
                continue
            # Normalize filament type for preset lookup (e.g., "PLA Basic" -> "PLA")
            base_type = tray_type.split()[0].upper()
            preset = presets.get(base_type)
            if not preset:
                continue

            temp = preset.get(temp_key, 55)
            hours = preset.get(hours_key, 12)

            # Conservative: lowest temp, longest duration
            if min_temp is None or temp < min_temp:
                min_temp = temp
            if max_hours is None or hours > max_hours:
                max_hours = hours
            if not filament_type:
                filament_type = base_type

        if min_temp is None:
            return None
        return (min_temp, max_hours or 12, filament_type)

    async def _check_auto_drying(self, db: AsyncSession, queue_items: list[PrintQueueItem], busy_printers: set[int]):
        """Start drying on idle printers based on humidity.

        Two modes (can both be enabled):
        - queue_drying_enabled: Dry between scheduled queue prints
        - ambient_drying_enabled: Dry any idle printer when humidity is high, regardless of queue
        """
        queue_drying_enabled = await self._get_bool_setting(db, "queue_drying_enabled")
        ambient_drying_enabled = await self._get_bool_setting(db, "ambient_drying_enabled")
        if not queue_drying_enabled and not ambient_drying_enabled:
            # Stop active drying on all printers if both features disabled
            if self._drying_in_progress:
                for pid in list(self._drying_in_progress):
                    logger.info("Auto-drying: printer %d - stopping, auto-drying disabled", pid)
                    await self._stop_drying(pid)
            return

        # Update drying state from printer status (handles backend restart)
        self._sync_drying_state()

        # Find printers with scheduled items (for queue drying mode)
        printers_with_scheduled: set[int] = set()
        printers_with_items: set[int] = set()
        for item in queue_items:
            if item.queue_id:
                printers_with_items.add(item.queue_id)
                if item.scheduled_time and not item.manual_start:
                    printers_with_scheduled.add(item.queue_id)

        # If only queue mode is on and no printers have scheduled items, stop drying
        if not ambient_drying_enabled and not printers_with_scheduled:
            for pid in list(self._drying_in_progress):
                logger.info("Auto-drying: printer %d - stopping, no scheduled prints in queue", pid)
                await self._stop_drying(pid)
            return

        # Get humidity threshold
        result = await db.execute(select(Settings).where(Settings.key == "ams_humidity_fair"))
        setting = result.scalar_one_or_none()
        humidity_threshold = int(setting.value) if setting else 60

        # Get drying presets
        presets = await self._get_drying_presets(db)

        # Determine if drying should be skipped for printers with pending items
        block_for_drying = await self._get_bool_setting(db, "queue_drying_block")

        # Get all active printers
        all_printers = await db.execute(select(Printer).where(Printer.is_active.is_(True)))
        for printer in all_printers.scalars():
            pid = printer.id
            if pid in busy_printers:
                logger.debug("Auto-drying: printer %d skipped - busy", pid)
                continue
            # In queue-only mode, only dry printers that have scheduled prints
            if not ambient_drying_enabled and pid not in printers_with_scheduled:
                if self._drying_in_progress.get(pid):
                    logger.info("Auto-drying: printer %d - stopping, no scheduled prints for this printer", pid)
                    await self._stop_drying(pid)
                logger.debug("Auto-drying: printer %d skipped - no scheduled prints", pid)
                continue
            # When block mode is on, don't START new drying on printers with pending items.
            # But allow already-drying printers through so humidity auto-stop logic still runs.
            if block_for_drying and pid in printers_with_items and not self._drying_in_progress.get(pid):
                logger.debug("Auto-drying: printer %d skipped - has pending items (block mode)", pid)
                continue
            if not printer_manager.is_connected(pid):
                logger.debug("Auto-drying: printer %d skipped - not connected", pid)
                continue
            if not self._is_printer_idle(pid):
                logger.debug("Auto-drying: printer %d skipped - not idle", pid)
                continue

            # Check if this printer supports drying
            state = printer_manager.get_status(pid)
            if not state:
                logger.debug("Auto-drying: printer %d skipped - no state", pid)
                continue
            model = printer_manager.get_model(pid)
            firmware = state.firmware_version
            if not supports_drying(model, firmware):
                logger.debug("Auto-drying: printer %d skipped - model %s does not support drying", pid, model)
                continue

            # Check each AMS unit from raw_data
            ams_list = state.raw_data.get("ams", [])
            logger.debug("Auto-drying: printer %d - checking %d AMS units", pid, len(ams_list))
            for ams_data in ams_list:
                module_type = str(ams_data.get("module_type") or "")
                ams_id = int(ams_data.get("id", 0))
                # Only n3f/n3s support drying
                if module_type not in ("n3f", "n3s"):
                    logger.debug("Auto-drying: printer %d AMS %d skipped - module_type=%s", pid, ams_id, module_type)
                    continue

                dry_time = int(ams_data.get("dry_time") or 0)

                # Read humidity - prefer humidity_raw (actual %) over humidity (index 1-5)
                humidity = None
                h_raw = ams_data.get("humidity_raw")
                if h_raw is not None:
                    try:
                        humidity = int(h_raw)
                    except (ValueError, TypeError):
                        pass
                if humidity is None:
                    h_idx = ams_data.get("humidity")
                    if h_idx is not None:
                        try:
                            humidity = int(h_idx)
                        except (ValueError, TypeError):
                            pass
                # Already drying - check if humidity dropped below threshold (with minimum drying time)
                if dry_time > 0:
                    if pid not in self._drying_in_progress:
                        # Drying we didn't start (manual or from before restart) - track but don't stop
                        self._drying_in_progress[pid] = time.monotonic()
                    started_at = self._drying_in_progress[pid]
                    elapsed = time.monotonic() - started_at
                    if humidity is not None and humidity <= humidity_threshold and elapsed >= self._min_drying_seconds:
                        logger.info(
                            "Auto-drying: printer %d AMS %d - humidity %d%% <= threshold %d%% after %dm, stopping drying",
                            pid,
                            ams_id,
                            humidity,
                            humidity_threshold,
                            int(elapsed / 60),
                        )
                        printer_manager.send_drying_command(pid, ams_id, temp=0, duration=0, mode=0)
                    else:
                        logger.debug(
                            "Auto-drying: printer %d AMS %d - drying (%dm left, humidity %s%%, elapsed %dm/%dm min)",
                            pid,
                            ams_id,
                            dry_time,
                            humidity,
                            int(elapsed / 60),
                            self._min_drying_seconds // 60,
                        )
                    continue

                # Humidity below threshold - no need to start drying
                if humidity is None or humidity <= humidity_threshold:
                    logger.debug(
                        "Auto-drying: printer %d AMS %d skipped - humidity %s <= threshold %d",
                        pid,
                        ams_id,
                        humidity,
                        humidity_threshold,
                    )
                    continue

                # Check cannot-dry reasons (power constraints etc.) — surface
                # the first human-readable blocker so support logs actually tell
                # the operator why auto-drying skipped, not just a bare code list.
                blocker = first_drying_blocking_reason(ams_data)
                if blocker is not None:
                    logger.debug(
                        "Auto-drying: printer %d AMS %d skipped — blocker code %d: %s",
                        pid,
                        ams_id,
                        blocker[0],
                        blocker[1],
                    )
                    continue

                # Get conservative drying params for mixed filaments
                trays = ams_data.get("tray", [])
                params = self._get_conservative_drying_params(trays, module_type, presets)
                if not params:
                    logger.debug(
                        "Auto-drying: printer %d AMS %d skipped - no drying-eligible filaments in trays", pid, ams_id
                    )
                    continue

                temp, duration_hours, filament_type = params

                # Start drying
                logger.info(
                    "Auto-drying: printer %d AMS %d - humidity %d%% > threshold %d%%, "
                    "starting %s drying at %d°C for %dh",
                    pid,
                    ams_id,
                    humidity,
                    humidity_threshold,
                    filament_type,
                    temp,
                    duration_hours,
                )
                success = printer_manager.send_drying_command(
                    pid, ams_id, temp, duration_hours, mode=1, filament=filament_type
                )
                if success:
                    self._drying_in_progress[pid] = time.monotonic()

    def _sync_drying_state(self):
        """Sync in-memory drying state with actual printer status.

        Handles backend restart - if a printer is drying but we don't know about it,
        update our state. If we think it's drying but it's not, clear it.
        """
        to_remove = []
        for pid in self._drying_in_progress:
            state = printer_manager.get_status(pid)
            if not state:
                to_remove.append(pid)
                continue
            # Check if any AMS unit is still drying
            ams_list = state.raw_data.get("ams", [])
            any_drying = any(int(a.get("dry_time") or 0) > 0 for a in ams_list)
            if not any_drying:
                to_remove.append(pid)
        for pid in to_remove:
            self._drying_in_progress.pop(pid, None)

    async def _stop_drying(self, printer_id: int):
        """Stop all active drying on a printer (print takes priority)."""
        state = printer_manager.get_status(printer_id)
        if not state:
            self._drying_in_progress.pop(printer_id, None)
            return

        ams_list = state.raw_data.get("ams", [])
        for ams_data in ams_list:
            dry_time = int(ams_data.get("dry_time") or 0)
            if dry_time > 0:
                ams_id = int(ams_data.get("id", 0))
                logger.info(
                    "Auto-drying: stopping drying on printer %d AMS %d - print takes priority",
                    printer_id,
                    ams_id,
                )
                printer_manager.send_drying_command(printer_id, ams_id, 0, 0, mode=0)
        self._drying_in_progress.pop(printer_id, None)

    async def _get_smart_plugs(self, db: AsyncSession, printer_id: int) -> list[SmartPlug]:
        """Get all smart plugs associated with a printer."""
        result = await db.execute(select(SmartPlug).where(SmartPlug.printer_id == printer_id))
        return list(result.scalars().all())

    async def _get_smart_plug(self, db: AsyncSession, printer_id: int) -> SmartPlug | None:
        """Get the first smart plug associated with a printer (backwards compat)."""
        plugs = await self._get_smart_plugs(db, printer_id)
        return plugs[0] if plugs else None

    async def _power_on_and_wait(self, plug: SmartPlug, printer_id: int, db: AsyncSession) -> bool:
        """Turn on smart plug and wait for printer to connect.

        Returns True if printer connected successfully within timeout.
        """
        # Get the appropriate service for the plug type (Tasmota or Home Assistant)
        service = await smart_plug_manager.get_service_for_plug(plug, db)

        # Check current plug state
        status = await service.get_status(plug)
        if not status.get("reachable"):
            logger.warning("Smart plug '%s' is not reachable", plug.name)
            return False

        # Turn on if not already on
        if status.get("state") != "ON":
            success = await service.turn_on(plug)
            if not success:
                logger.warning("Failed to turn on smart plug '%s'", plug.name)
                return False
            logger.info("Powered on smart plug '%s' for printer %s", plug.name, printer_id)

        # Get printer from database for connection
        result = await db.execute(select(Printer).where(Printer.id == printer_id))
        printer = result.scalar_one_or_none()
        if not printer:
            logger.error("Printer %s not found in database", printer_id)
            return False

        # Wait for printer to boot (give it some time before trying to connect)
        logger.info("Waiting 30s for printer %s to boot...", printer_id)
        await asyncio.sleep(30)

        # Try to connect to the printer periodically
        elapsed = 30  # Already waited 30s
        while elapsed < self._power_on_wait_time:
            # Try to connect
            logger.info("Attempting to connect to printer %s...", printer_id)
            try:
                connected = await printer_manager.connect_printer(printer)
                if connected:
                    logger.info("Printer %s connected after %ss", printer_id, elapsed)
                    # Give it a moment to stabilize and get status
                    await asyncio.sleep(5)
                    return True
            except Exception as e:
                logger.debug("Connection attempt failed: %s", e)

            await asyncio.sleep(self._power_on_check_interval)
            elapsed += self._power_on_check_interval
            logger.debug("Waiting for printer %s to connect... (%ss)", printer_id, elapsed)

        logger.warning("Printer %s did not connect within %ss after power on", printer_id, self._power_on_wait_time)
        return False

    async def _power_off_if_needed(self, db: AsyncSession, item: PrintQueueItem):
        """Power off printer if auto_off_after is enabled (waits for cooldown)."""
        if not item.auto_off_after:
            return

        plugs = await self._get_smart_plugs(db, item.queue_id)
        enabled_plugs = [p for p in plugs if p.enabled]
        if enabled_plugs:
            logger.info("Auto-off: Waiting for printer %s to cool down before power off...", item.queue_id)
            # Wait for cooldown (up to 10 minutes)
            await printer_manager.wait_for_cooldown(item.queue_id, target_temp=50.0, timeout=600)
            for plug in enabled_plugs:
                logger.info("Auto-off: Powering off printer %s via plug '%s'", item.queue_id, plug.name)
                service = await smart_plug_manager.get_service_for_plug(plug, db)
                await service.turn_off(plug)

    async def _get_job_name(self, db: AsyncSession, item: PrintQueueItem) -> str:
        """Get a human-readable name for a queue item."""
        if item.archive_id:
            result = await db.execute(select(PrintArchive).where(PrintArchive.id == item.archive_id))
            archive = result.scalar_one_or_none()
            if archive:
                return archive.filename.replace(".gcode.3mf", "").replace(".3mf", "")
        if item.library_file_id:
            result = await db.execute(select(LibraryFile).where(LibraryFile.id == item.library_file_id))
            library_file = result.scalar_one_or_none()
            if library_file:
                return library_file.filename.replace(".gcode.3mf", "").replace(".3mf", "")
        return f"Job #{item.id}"

    async def _get_printer(self, db: AsyncSession, printer_id: int) -> Printer | None:
        """Get printer by ID."""
        result = await db.execute(select(Printer).where(Printer.id == printer_id))
        return result.scalar_one_or_none()

    async def _fail_item(self, db: AsyncSession, item: PrintQueueItem, error_message: str) -> None:
        """Mark item as failed and set queue to error state."""
        from backend.app.services.queue_counters import set_queue_error, update_queue_counters

        item.status = "failed"
        item.error_message = error_message
        item.completed_at = datetime.now(timezone.utc)
        await set_queue_error(db, item.queue_id, failed_item_id=item.id)
        await update_queue_counters(db, item.queue_id)
        await db.commit()

    async def _start_print(self, db: AsyncSession, item: PrintQueueItem):
        """Upload file and start print for a queue item.

        Supports two sources:
        - archive_id: Print from an existing archive
        - library_file_id: Print from a library file (file manager)
        """
        logger.info("Starting queue item %s", item.id)

        # Get printer first (needed for both paths)
        result = await db.execute(select(Printer).where(Printer.id == item.queue_id))
        printer = result.scalar_one_or_none()
        if not printer:
            await self._fail_item(db, item, "Printer not found")
            logger.error("Queue item %s: Printer %s not found", item.id, item.queue_id)
            await self._power_off_if_needed(db, item)
            return

        # Check printer is connected
        if not printer_manager.is_connected(item.queue_id):
            await self._fail_item(db, item, "Printer not connected")
            logger.error("Queue item %s: Printer %s not connected", item.id, item.queue_id)
            await self._power_off_if_needed(db, item)
            return

        # re-Connect MQTT if stalled
        if not await printer_manager.ensure_fresh_connection_for_printer(printer):
            await self._fail_item(db, item, "Printer not connected")
            logger.error("Queue item %s: Printer %s not connected", item.id, item.queue_id)
            await self._power_off_if_needed(db, item)
            return

        # Determine source: archive or library file. file_path is kept so we
        # can still do the "file exists on disk" guard before delegating.
        archive = None
        library_file = None
        file_path: Path | None = None

        if item.archive_id:
            result = await db.execute(select(PrintArchive).where(PrintArchive.id == item.archive_id))
            archive = result.scalar_one_or_none()
            if not archive:
                await self._fail_item(db, item, "Archive not found")
                logger.error("Queue item %s: Archive %s not found", item.id, item.archive_id)
                await self._power_off_if_needed(db, item)
                return

            file_path = settings.base_dir / archive.file_path

        elif item.library_file_id:
            result = await db.execute(select(LibraryFile).where(LibraryFile.id == item.library_file_id))
            library_file = result.scalar_one_or_none()
            if not library_file:
                await self._fail_item(db, item, "Library file not found")
                logger.error("Queue item %s: Library file %s not found", item.id, item.library_file_id)
                await self._power_off_if_needed(db, item)
                return
            lib_path = Path(library_file.file_path)
            file_path = lib_path if lib_path.is_absolute() else settings.base_dir / library_file.file_path

        else:
            # Neither archive nor library file specified
            await self._fail_item(db, item, "No source file specified")
            logger.error("Queue item %s: No archive_id or library_file_id specified", item.id)
            await self._power_off_if_needed(db, item)
            return

        # Check file exists on disk (fast fail before any dispatch work).
        if not file_path.exists():
            await self._fail_item(db, item, "Source file not found on disk")
            logger.error("Queue item %s: File not found: %s", item.id, file_path)
            await self._power_off_if_needed(db, item)
            return

        # Flip item to "printing" BEFORE dispatch starts so that a crash
        # mid-FTP doesn't leave the item re-dispatchable. Backed by
        # set_queue_printing to keep the printer_queues counters in lockstep.
        from backend.app.services.queue_counters import set_queue_printing, update_queue_counters

        item.status = "printing"
        item.started_at = datetime.now(timezone.utc)
        await set_queue_printing(db, item.queue_id, item.id)
        await update_queue_counters(db, item.queue_id)
        await db.commit()

        # Parse per-item options into the shape background_dispatch expects.
        ams_mapping: list[int] | None = None
        if item.ams_mapping:
            try:
                ams_mapping = json.loads(item.ams_mapping)
            except json.JSONDecodeError:
                logger.warning("Queue item %s: Invalid AMS mapping JSON, ignoring", item.id)

        swap_events: list[str] = []
        if item.execute_swap_macros and item.swap_macro_events:
            try:
                swap_events = json.loads(item.swap_macro_events) if isinstance(item.swap_macro_events, str) else []
            except (ValueError, TypeError):
                swap_events = []

        options: dict[str, Any] = {
            "mesh_mode_fast_check": item.mesh_mode_fast_check,
            "ams_mapping": ams_mapping,
            "plate_id": item.plate_id or 1,
            "bed_levelling": item.bed_levelling,
            "flow_cali": item.flow_cali,
            "layer_inspect": item.layer_inspect,
            "timelapse": item.timelapse,
            "use_ams": item.use_ams,
            "execute_swap_macros": item.execute_swap_macros,
            "swap_macro_events": swap_events,
        }

        if archive:
            dispatch_kind: Literal["reprint_archive", "print_library_file"] = "reprint_archive"
            dispatch_source_id = archive.id
            dispatch_source_name = archive.filename
        elif library_file:
            dispatch_kind = "print_library_file"
            dispatch_source_id = library_file.id
            dispatch_source_name = library_file.filename
        else:
            # Should have been caught above, but belt-and-braces.
            await self._fail_item(db, item, "No source file specified")
            await self._power_off_if_needed(db, item)
            return

        # Delegate the full patch → archive → FTP → register_expected → MQTT
        # start pipeline to the background-dispatch runner. It sets
        # ``job.outcome`` before returning. Queue-item awareness wires
        # ``item.archive_id`` inside the same txn as archive creation, so the
        # row we already flipped to "printing" has a valid archive_id before
        # the printer actually reports RUNNING.
        # Lazy import — background_dispatch + print_scheduler have a two-way
        # relationship (dispatcher calls back into scheduler's stagger helper).
        from backend.app.services.background_dispatch import background_dispatch

        job_name_short = dispatch_source_name.replace(".gcode.3mf", "").replace(".3mf", "")

        try:
            outcome = await background_dispatch.run_from_queue_item(
                kind=dispatch_kind,
                source_id=dispatch_source_id,
                source_name=dispatch_source_name,
                printer_id=item.queue_id,
                printer_name=printer.name,
                options=options,
                requested_by_user_id=item.created_by_id,
                requested_by_username=None,
                project_id=item.project_id,
                queue_item_id=item.id,
            )
        except Exception as e:  # pragma: no cover — belt-and-braces
            logger.exception("Queue item %s: run_from_queue_item raised: %s", item.id, e)
            await self._fail_item(db, item, f"Dispatch error: {e}")
            await self._power_off_if_needed(db, item)
            return

        if not outcome.get("success"):
            err = outcome.get("error") or "Dispatch failed"
            await self._fail_item(db, item, err)
            await notification_service.on_queue_job_failed(
                job_name=job_name_short,
                printer_id=printer.id,
                printer_name=printer.name,
                reason=err,
                db=db,
            )
            await self._power_off_if_needed(db, item)
            return

        # Refresh the item so scheduler sees the archive_id the dispatcher
        # just assigned (for library_file dispatches).
        await db.refresh(item)
        logger.info("Queue item %s: Dispatch succeeded — archive_id=%s", item.id, item.archive_id)

        # Watchdog for pre_state → transition, swap-aware. Upstream #1078 —
        # see the big comment block that was here in the pre-refactor code.
        _post_status = printer_manager.get_status(item.queue_id)
        _pre_state = _post_status.state if _post_status else None
        _pre_subtask_id = _post_status.subtask_id if _post_status else None

        # Hold the printer against further dispatches until the watchdog
        # confirms the printer transitioned (or until the hard timeout).
        # Prevents multi-plate batches from triple-dispatching onto the same
        # H2D Pro while it digests the first project_file (#1157, ported from
        # upstream v0.2.4b1). Marked even when pre_state is None so a
        # disconnected-at-dispatch printer still gets a time-based hold.
        self._mark_printer_dispatched(item.queue_id, _pre_state, _pre_subtask_id)

        if _pre_state:
            asyncio.create_task(
                self._watchdog_print_start(
                    item.id,
                    item.queue_id,
                    _pre_state,
                    _pre_subtask_id,
                    swap_start_fired="swap_mode_start" in swap_events,
                )
            )

        # Estimated time for the notification — prefer archive metadata,
        # fall back to library_file.
        estimated_time = None
        if item.archive_id:
            arch_row = await db.get(PrintArchive, item.archive_id)
            if arch_row is not None and arch_row.print_time_seconds:
                estimated_time = arch_row.print_time_seconds
        if estimated_time is None and library_file and library_file.print_time_seconds:
            estimated_time = library_file.print_time_seconds

        await notification_service.on_queue_job_started(
            job_name=job_name_short,
            printer_id=printer.id,
            printer_name=printer.name,
            db=db,
            estimated_time=estimated_time,
        )

        try:
            from backend.app.services.mqtt_relay import mqtt_relay

            await mqtt_relay.on_queue_job_started(
                job_id=item.id,
                filename=dispatch_source_name,
                printer_id=printer.id,
                printer_name=printer.name,
                printer_serial=printer.serial_number,
            )
        except Exception:
            pass  # Don't fail the scheduler if the relay misbehaves.

    @staticmethod
    async def _watchdog_print_start(
        queue_item_id: int,
        printer_id: int,
        pre_state: str,
        pre_subtask_id: str | None = None,
        swap_start_fired: bool = False,
        timeout: float = 90.0,
        poll_interval: float = 3.0,
    ) -> None:
        """Revert a queue item if the printer never acknowledges the start command.

        Optimistically we mark the queue item as "printing" right after the MQTT
        project_file publish succeeds locally. If the printer drops/ignores the
        command (half-broken MQTT session — #887/#936), the state never
        transitions and the item would otherwise stay stuck in "printing"
        forever (#967).

        Watchdog exits early on EITHER of two "command landed" signals:
          * ``gcode_state`` advances past ``pre_state`` — the classic signal.
          * ``subtask_id`` advances past ``pre_subtask_id`` — the printer
            echoes the submission_id we minted (``bambu_mqtt._publish_project``,
            #1042) in its next push_status.subtask_id. On H2D Pro firmware
            01.01.00.00 the state stays at FINISH 48–55 s after accepting
            the command, but subtask_id echoes back within the first push —
            without this guard the old 45 s timeout reverted items the
            printer had already started physically printing, which then
            re-dispatched on the next scheduler tick and looked like a
            reprint (upstream #1078).

        ``timeout`` bumped to 90 s (was 45 s) as belt-and-braces for printers
        that neither flip state nor echo subtask_id inside the window —
        genuinely half-broken sessions still revert + force-reconnect as
        before, just a bit later.

        ``swap_start_fired`` is the BamDude-specific guard: when a swap_mode_start
        macro already ran successfully on the real printer before start_print,
        a revert + re-dispatch would re-fire it and cause a double physical
        table swap. In that case we keep the item in "printing", log a louder
        warning telling the operator to intervene manually, and still
        force-reconnect the MQTT session so subsequent commands land.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            status = printer_manager.get_status(printer_id)
            if not status:
                # Printer disconnected — don't mess with the DB. Drop the
                # in-memory dispatch hold (#1157) too so a fresh dispatch can
                # retry once the printer comes back; the hard timeout would
                # otherwise hold the printer unnecessarily.
                scheduler._release_dispatch_hold(printer_id)
                return
            if status.state != pre_state:
                # Printer picked up the job (state change) — release the
                # post-dispatch hold so the next pending item for this printer
                # can be evaluated normally (#1157).
                scheduler._release_dispatch_hold(printer_id)
                return
            if pre_subtask_id and status.subtask_id and status.subtask_id != pre_subtask_id:
                # subtask_id advanced — command landed even though state
                # hasn't flipped yet (#1157 release).
                scheduler._release_dispatch_hold(printer_id)
                return

        if swap_start_fired:
            logger.error(
                "Queue item %s: printer %d did not respond to print command within "
                "%.0fs (state still %s), BUT swap_mode_start already fired — NOT "
                "reverting to pending to avoid double table swap. Operator intervention "
                "required (#967 + swap-mode)",
                queue_item_id,
                printer_id,
                timeout,
                pre_state,
            )
        else:
            # No swap side-effects on the physical printer — safe to revert.
            # Drop the in-memory dispatch hold (#1157) so the retry isn't
            # blocked by it.
            scheduler._release_dispatch_hold(printer_id)
            async with async_session() as db:
                item = await db.get(PrintQueueItem, queue_item_id)
                if not item or item.status != "printing":
                    return  # Already moved on (completed/cancelled/etc.)
                item.status = "pending"
                item.started_at = None
                await db.commit()
                logger.warning(
                    "Queue item %s: printer %d did not respond to print command within "
                    "%.0fs (state still %s) — reverted to 'pending' for retry (#967)",
                    queue_item_id,
                    printer_id,
                    timeout,
                    pre_state,
                )
            # Drop any swap config that was registered post-start_print so a
            # subsequent dispatch can arm it fresh.
            try:
                from backend.app.main import _active_swap_config

                _active_swap_config.pop(printer_id, None)
            except Exception:  # pragma: no cover — import failure is non-fatal
                pass

        # Same half-broken-session recovery as background_dispatch: force the
        # MQTT client to reconnect so the next dispatch lands without a power cycle.
        client = printer_manager.get_client(printer_id)
        if client and hasattr(client, "force_reconnect_stale_session"):
            client.force_reconnect_stale_session(
                f"queue print command unacknowledged after {timeout:.0f}s (state still {pre_state})"
            )


# Global scheduler instance
scheduler = PrintScheduler()
