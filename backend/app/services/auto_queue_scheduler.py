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
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.settings import Settings
from backend.app.services.auto_queue_ams import compute_ams_mapping_for_printer
from backend.app.services.auto_queue_eligibility import find_eligible_printer, offline_candidates_for

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

    # A stalled queue re-logs itself this often even when nothing has changed, so
    # a support bundle collected hours into the stall still contains the reason
    # rather than one line from when it began.
    _stall_reminder_seconds = 600

    def __init__(self) -> None:
        self._running = False
        # Signature of the last logged stall (reasons + busy set). Kept so a
        # queue that cannot move logs once per *change* instead of once per tick
        # — at 30s a stuck queue would otherwise write 120 identical lines an
        # hour and bury everything else in the support log.
        self._last_stall: tuple[str, float] | None = None

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

            # 1. Build busy set: a printer is "off-limits for new auto-routing"
            #    when EITHER its queue is currently printing OR its queue
            #    already holds a pending item (regardless of how that item
            #    got there — manual queue, scheduled, prior auto-route).
            #
            #    The status='printing' clause alone is not enough: between
            #    auto-queue tick N (which assigns items 1..K to K printers as
            #    pending PrintQueueItem rows) and PrintScheduler's next tick
            #    (which actually flips PrinterQueue.status to 'printing' as
            #    its synchronous prep walks the items in queue_id order),
            #    there's a window where some printers have already flipped
            #    and the lagging ones haven't. If auto-queue tick N+1 fires
            #    inside that window it sees the lagging printers as "free"
            #    and double-stacks the next pending items onto them — every
            #    new auto item lands on the same lagging printer (highest id,
            #    last in PrintScheduler's per-tick prep order). Including
            #    "has any pending PrintQueueItem" in the busy set closes the
            #    gap: each auto-queue tick can place at most one new item
            #    per printer, and the next placement on that printer waits
            #    until the queue actually drains.
            busy_q1 = await db.execute(select(PrinterQueue.printer_id).where(PrinterQueue.status == "printing"))
            busy_q2 = await db.execute(
                select(PrinterQueue.printer_id)
                .join(PrintQueueItem, PrintQueueItem.queue_id == PrinterQueue.id)
                .where(PrintQueueItem.status == "pending")
                .distinct()
            )
            busy_printers: set[int] = {pid for (pid,) in busy_q1.all()} | {pid for (pid,) in busy_q2.all()}

            # 2. Fetch pending auto items in scheduling order
            pending = await self._fetch_pending(db, sjf)
            items = list(pending)
            if not items:
                return

            logger.debug("AutoQueueScheduler: %d pending items, busy_printers=%s", len(items), busy_printers)

            # 3. Iterate and assign
            placed = 0
            blocked: list[str] = []
            # The first item that could not be placed, kept to name *something*
            # concrete in the notification below — "4 jobs are waiting" is far
            # less useful than the name of one of them plus the reason.
            first_blocked: tuple[AutoQueueItem, str] | None = None
            # At most one printer is woken per pass — see _wake_offline_printer.
            woke_one = False
            for item in items:
                printer, reason = await find_eligible_printer(db, item, busy_printers)
                if printer is None:
                    if not woke_one:
                        woke_one = await self._wake_offline_printer(db, item, busy_printers)
                    # The reason has always been computed and stored on the row;
                    # it was never logged, which is why three support bundles
                    # from a farm whose "queue stopped moving" contained no
                    # evidence at all. INFO on change only — see _last_stall.
                    if reason and item.waiting_reason != reason:
                        logger.info("Auto item %s not placed: %s", item.id, reason)
                        item.waiting_reason = reason
                    blocked.append(reason or "no reason reported")
                    if first_blocked is None:
                        first_blocked = (item, reason or "no reason reported")
                    continue

                try:
                    await self._assign(db, item, printer, prefer_lowest=prefer_lowest)
                except Exception:
                    logger.exception("Failed to assign auto item %s to printer %s", item.id, printer.id)
                    continue

                busy_printers.add(printer.id)
                placed += 1

                if sjf:
                    await self._mark_jumped_peers(db, item)

            announce = self._log_stall(items, placed, blocked, busy_printers)
            await db.commit()

            # After the commit: the notification is a side effect on the outside
            # world, and it should never be sent describing a state that then
            # failed to persist.
            if announce and first_blocked is not None:
                await self._notify_stall(db, *first_blocked)

    def _log_stall(self, items: list, placed: int, blocked: list[str], busy_printers: set[int]) -> bool:
        """Say once, at INFO, that a tick could place nothing — and why.

        Without this the only trace of a stalled auto-queue was a DEBUG line
        nobody has enabled, so an INFO-level support bundle showed a healthy
        application and an idle farm with work waiting. Throttled on a signature
        of (reasons, busy printers) and re-stated every
        ``_stall_reminder_seconds`` so a long stall stays visible to whoever
        collects the log later.

        Returns True when this tick announced a *new* cause. The caller notifies
        on that only: the periodic reminder exists for the log file, and firing a
        Telegram message every ten minutes for a stall the operator already knows
        about is how people turn notifications off.
        """
        if placed or not items:
            self._last_stall = None
            return False

        signature = f"{sorted(set(blocked))}|{sorted(busy_printers)}"
        now = asyncio.get_event_loop().time()
        is_new = True
        if self._last_stall is not None:
            previous, when = self._last_stall
            if previous == signature:
                is_new = False
                if now - when < self._stall_reminder_seconds:
                    return False

        logger.info(
            "Auto-queue placed nothing this tick: %d item(s) waiting, busy printers=%s, reasons=%s",
            len(items),
            sorted(busy_printers) or "none",
            sorted(set(blocked)),
        )
        self._last_stall = (signature, now)
        return is_new

    async def _notify_stall(self, db: AsyncSession, item: AutoQueueItem, reason: str) -> None:
        """Tell the operator that auto-queue stopped placing work, and why.

        ``on_queue_job_waiting`` has existed since the notification system was
        built — a provider column defaulting to enabled, a per-chat Telegram
        toggle, en+uk templates carrying ``{waiting_reason}`` — and nothing has
        ever called it. The operator sees the switch on and receives nothing.

        On an unattended farm that silence *is* the failure: one print that ends
        badly arms the plate-clear gate, auto-queue stops routing to that
        printer, and the machine leaves the rotation with nobody told. Fires once
        per stall cause, and can never break the tick.
        """
        from backend.app.services.notification_service import notification_service

        try:
            await notification_service.on_queue_job_waiting(
                job_name=await self._job_name(db, item),
                target_model=item.target_model or "any model",
                waiting_reason=reason,
                db=db,
            )
        except Exception:
            logger.exception("Failed to send the queue_job_waiting notification for auto item %s", item.id)

    async def _job_name(self, db: AsyncSession, item: AutoQueueItem) -> str:
        """Best-effort display name, resolved by id rather than by relationship.

        ``_fetch_pending`` selects bare rows, so reaching for ``item.archive``
        here would lazy-load under asyncio and raise MissingGreenlet.
        """
        if item.archive_id:
            row = (
                await db.execute(
                    select(PrintArchive.print_name, PrintArchive.filename).where(PrintArchive.id == item.archive_id)
                )
            ).first()
            if row:
                return row[0] or row[1] or f"Auto item #{item.id}"
        if item.library_file_id:
            row = (await db.execute(select(LibraryFile.filename).where(LibraryFile.id == item.library_file_id))).first()
            if row:
                return row[0] or f"Auto item #{item.id}"
        return f"Auto item #{item.id}"

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

    # printer_id -> monotonic deadline before which we will not try to wake it
    # again. See _wake_offline_printer for why one window covers both outcomes.
    _wake_cooldowns: dict[int, float] = {}
    _WAKE_COOLDOWN_SECONDS = 600.0

    async def _wake_offline_printer(self, db, item, busy_printers: set[int]) -> bool:
        """Switch on one printer this item could run on, if they are all off.

        ⚠️ **The gap this closes.** A job aimed at a printer *class* with every
        printer of that class switched off used to sit for ever: routing needs
        live filament state, a printer that is off reports none, so no candidate
        is eligible, so the item never reaches a per-printer queue — and the
        per-printer queue is the only thing that powers a printer on. The same
        file pinned to a specific printer wakes it within one pass.

        ⚠️ **One printer per pass**, so a shelf of eight does not all come up at
        once for one job. Several queued jobs bring several printers up over the
        following minutes, which is the behaviour worth having.

        ⚠️ **One cooldown covers success and failure alike**, unlike upstream's,
        because we deliberately do *not* wait for the boot. Waiting would block
        the distributor for minutes; instead the next pass simply finds the
        printer connected and routes to it normally. That means success is not
        observable here, so a single window is the honest rule: having asked a
        plug to turn on, asking again 30 seconds later achieves nothing whether
        it worked or not.

        ⚠️ **A printer we failed to wake is NOT added to ``busy_printers``.** It
        is off, not busy — labelling it busy would misdescribe it in every later
        item's waiting reason, and an all-busy reason is treated as needing no
        user action, so it would suppress the notification too.
        """
        import time as _time

        from backend.app.models.smart_plug import SmartPlug
        from backend.app.services.smart_plug_manager import smart_plug_manager

        candidates = await offline_candidates_for(db, item, busy_printers)
        if not candidates:
            return False

        now = _time.monotonic()
        for printer in candidates:
            # Expire on read, so a live entry can never be overwritten by a
            # later success and a stale one costs nothing.
            deadline = self._wake_cooldowns.get(printer.id)
            if deadline is not None and deadline > now:
                continue

            plugs = (
                (
                    await db.execute(
                        select(SmartPlug).where(
                            SmartPlug.printer_id == printer.id,
                            SmartPlug.enabled.is_(True),
                            SmartPlug.auto_on.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not plugs:
                continue

            self._wake_cooldowns[printer.id] = now + self._WAKE_COOLDOWN_SECONDS
            logger.info(
                "Auto item %s has no online %s; powering on %s to receive it",
                item.id,
                item.target_model,
                printer.name,
            )
            try:
                for plug in plugs:
                    service = await smart_plug_manager.get_service_for_plug(plug, db)
                    await service.turn_on(plug)
            except Exception:
                logger.exception("Failed to power on %s for auto item %s", printer.name, item.id)
                return False
            # Deliberately not touching busy_printers, and not waiting: the next
            # pass routes to it once it is up.
            return True
        return False

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
            # Carried onto the per-printer row so the gate is re-checked at
            # dispatch: eligibility only proves the printer was clean at the
            # moment of routing, and another print can fail in between.
            require_previous_success=item.require_previous_success,
            auto_off_after=item.auto_off_after,
            ams_mapping=ams_mapping_json,
            plate_id=item.plate_id,
            bed_levelling=item.bed_levelling,
            flow_cali=item.flow_cali,
            layer_inspect=item.layer_inspect,
            timelapse=item.timelapse,
            timelapse_storage=item.timelapse_storage,
            use_ams=item.use_ams,
            mesh_mode_fast_check=item.mesh_mode_fast_check,
            gcode_injection=item.gcode_injection,
            execute_swap_macros=item.execute_swap_macros,
            swap_macro_events=item.swap_macro_events,
            selected_macro_ids=item.selected_macro_ids,
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
