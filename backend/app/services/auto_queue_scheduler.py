"""AutoQueueScheduler — the router that sits above per-printer queues.

Runs as a background asyncio task (started in ``main.py`` lifespan).
On each tick it:

1. Snapshots ``busy_printers`` from PrinterQueue rows currently
   ``status='printing'`` (mirrors PrintScheduler's race-prevention
   pattern).
2. Reads pending AutoQueueItem rows ordered by SJF + been_jumped if
   ``queue_shortest_first`` setting is true, else by position.
3. For each item: calls ``find_eligible_printer`` to pick an idle
   printer that matches model + filaments + colors. If found, assigns
   the item by copying it into that printer's print_queue (computing
   AMS mapping from current printer state). The per-printer scheduler
   then dispatches it on its next tick (~immediately).
4. If no printer matches, updates ``waiting_reason`` so the user can
   see why the item is stuck.
5. After a successful assign, when SJF is enabled, marks longer
   pending peers (same target_model, earlier position, longer or
   unknown print time) as ``been_jumped=True`` to prevent starvation.

Dispatch happens via the existing per-printer flow — once
``print_queue`` has the new row, ``PrintScheduler.check_queue()`` and
``BackgroundDispatch`` take over with full plate-clear / stagger /
swap-macro / drying support intact.

Design rationale + open-questions resolved in
``temp/auto-queue-adaptation-variants.md`` §11-§12.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import async_session
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.settings import Settings
from backend.app.services.auto_queue_ams import compute_ams_mapping_for_printer
from backend.app.services.auto_queue_eligibility import find_eligible_printer

logger = logging.getLogger(__name__)


SJF_SETTING_KEY = "queue_shortest_first"
PREFER_LOWEST_SETTING_KEY = "prefer_lowest_filament"


async def _get_bool_setting(db: AsyncSession, key: str, default: bool = False) -> bool:
    """Read a boolean setting from the ``settings`` table.

    Same shape as upstream's ``PrintScheduler._get_bool_setting``.
    """
    result = await db.execute(select(Settings).where(Settings.key == key))
    setting = result.scalar_one_or_none()
    if setting:
        return setting.value.lower() == "true"
    return default


class AutoQueueScheduler:
    """Background loop that routes AutoQueueItems to idle printers."""

    _check_interval = 30  # seconds — same cadence as PrintScheduler

    def __init__(self) -> None:
        self._running = False

    async def run(self) -> None:
        """Main loop. Started from ``main.py`` lifespan via asyncio.create_task."""
        self._running = True
        logger.info("Auto-queue scheduler started (interval=%ds)", self._check_interval)
        while self._running:
            try:
                await self.tick()
            except Exception:
                # Never let one bad tick kill the loop — log and continue.
                logger.exception("AutoQueueScheduler tick failed")
            await asyncio.sleep(self._check_interval)

    def stop(self) -> None:
        self._running = False
        logger.info("Auto-queue scheduler stopped")

    async def tick(self) -> None:
        """Single iteration: assign pending auto items to eligible printers."""
        async with async_session() as db:
            sjf = await _get_bool_setting(db, SJF_SETTING_KEY)
            prefer_lowest = await _get_bool_setting(db, PREFER_LOWEST_SETTING_KEY)

            # 1. Build busy set from per-printer queues currently dispatching/printing.
            #    PrinterQueue.status='printing' is the authoritative "this printer is
            #    occupied" marker — flipped at dispatch time by the existing
            #    per-printer flow. Reading it here avoids racing with PrintScheduler.
            busy_result = await db.execute(select(PrinterQueue.printer_id).where(PrinterQueue.status == "printing"))
            busy_printers: set[int] = {pid for (pid,) in busy_result.all()}

            # 2. Fetch pending auto items in scheduling order
            pending = await self._fetch_pending(db, sjf)
            items = list(pending)
            if not items:
                return

            logger.debug("AutoQueueScheduler: %d pending items, busy_printers=%s", len(items), busy_printers)

            # 3. Iterate and assign
            for item in items:
                printer, reason = await find_eligible_printer(db, item, busy_printers)
                if printer is None:
                    if reason and item.waiting_reason != reason:
                        item.waiting_reason = reason
                    continue

                try:
                    await self._assign(db, item, printer, prefer_lowest=prefer_lowest)
                except Exception:
                    logger.exception("Failed to assign auto item %s to printer %s", item.id, printer.id)
                    continue

                busy_printers.add(printer.id)

                if sjf:
                    await self._mark_jumped_peers(db, item)

            await db.commit()

    async def _fetch_pending(self, db: AsyncSession, sjf: bool):
        """Return pending auto items in scheduling order.

        SJF on:  ``ORDER BY target_model, been_jumped DESC,
                  print_time_seconds ASC NULLS LAST, position``
        SJF off: ``ORDER BY position``
        """
        now = datetime.now(timezone.utc)
        base = (
            select(AutoQueueItem)
            .where(AutoQueueItem.status == "pending")
            .where(AutoQueueItem.cancelled_at.is_(None))
            .where(AutoQueueItem.manual_start.is_(False))
            .where(or_(AutoQueueItem.scheduled_time.is_(None), AutoQueueItem.scheduled_time <= now))
        )
        if sjf:
            stmt = base.order_by(
                AutoQueueItem.target_model,
                AutoQueueItem.been_jumped.desc(),
                AutoQueueItem.print_time_seconds.asc().nullslast(),
                AutoQueueItem.position,
            )
        else:
            stmt = base.order_by(AutoQueueItem.position)
        result = await db.execute(stmt)
        return result.scalars().all()

    async def _assign(
        self,
        db: AsyncSession,
        item: AutoQueueItem,
        printer: Printer,
        prefer_lowest: bool = False,
    ) -> PrintQueueItem:
        """Copy auto item into the printer's print_queue and mark assigned.

        Computes AMS mapping from current printer state (mirrors
        upstream's "compute on dispatch" approach — overrides applied here).
        """
        # 1. Compute AMS mapping for this specific printer
        ams_mapping = await compute_ams_mapping_for_printer(db, printer.id, item, prefer_lowest=prefer_lowest)
        ams_mapping_json = json.dumps(ams_mapping) if ams_mapping is not None else None

        # 2. Find target queue for this printer
        queue_result = await db.execute(select(PrinterQueue).where(PrinterQueue.printer_id == printer.id))
        printer_queue = queue_result.scalar_one_or_none()
        if printer_queue is None:
            raise RuntimeError(f"Printer {printer.id} has no PrinterQueue row")

        # 3. Compute next position in the per-printer queue
        max_pos = await db.scalar(
            select(func.coalesce(func.max(PrintQueueItem.position), 0)).where(
                PrintQueueItem.queue_id == printer_queue.id
            )
        )
        next_pos = (max_pos or 0) + 1

        # 4. Build the new per-printer item with copied options
        new_item = PrintQueueItem(
            queue_id=printer_queue.id,
            archive_id=item.archive_id,
            library_file_id=item.library_file_id,
            project_id=item.project_id,
            position=next_pos,
            scheduled_time=item.scheduled_time,
            manual_start=False,
            auto_off_after=item.auto_off_after,
            ams_mapping=ams_mapping_json,
            plate_id=item.plate_id,
            bed_levelling=item.bed_levelling,
            flow_cali=item.flow_cali,
            layer_inspect=item.layer_inspect,
            timelapse=item.timelapse,
            use_ams=item.use_ams,
            mesh_mode_fast_check=item.mesh_mode_fast_check,
            execute_swap_macros=item.execute_swap_macros,
            swap_macro_events=item.swap_macro_events,
            status="pending",
            batch_id=item.batch_id,
            created_by_id=item.created_by_id,
            source_auto_item_id=item.id,
        )
        db.add(new_item)
        await db.flush()

        # 5. Mark auto item as assigned (back-reference + timestamp + clear reason)
        item.status = "assigned"
        item.assigned_to_item_id = new_item.id
        item.assigned_at = datetime.now(timezone.utc)
        item.waiting_reason = None

        logger.info(
            "Auto item %s assigned to printer %s (queue %s, position %d, new pq item %s)",
            item.id,
            printer.id,
            printer_queue.id,
            next_pos,
            new_item.id,
        )
        return new_item

    async def _mark_jumped_peers(self, db: AsyncSession, started_item: AutoQueueItem) -> None:
        """SJF starvation guard — mark peers that were skipped.

        Same logic as upstream: items in the same target_model group with
        earlier position whose print_time is unknown or longer than the
        just-started one get ``been_jumped=True`` (sticky).
        """
        if started_item.print_time_seconds is None:
            return
        await db.execute(
            update(AutoQueueItem)
            .where(AutoQueueItem.status == "pending")
            .where(AutoQueueItem.target_model == started_item.target_model)
            .where(AutoQueueItem.position < started_item.position)
            .where(AutoQueueItem.been_jumped.is_(False))
            .where(
                or_(
                    AutoQueueItem.print_time_seconds.is_(None),
                    AutoQueueItem.print_time_seconds > started_item.print_time_seconds,
                )
            )
            .values(been_jumped=True)
        )


# Module-level singleton, mirroring print_scheduler pattern
auto_queue_scheduler = AutoQueueScheduler()
