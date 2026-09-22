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
from backend.app.core.websocket import ws_manager
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.schemas.calibration_mode import normalize_mode
from backend.app.services import queue_rebalance
from backend.app.services.auto_queue_eligibility import busy_printer_ids, find_eligible_printer, offline_candidates_for
from backend.app.services.filament_intake import fail_auto_source, read_item_requirements
from backend.app.services.filament_policy import auto_policy, serialize_policy
from backend.app.services.filament_preflight import feed_signature
from backend.app.services.filament_requirements import PrintRequirementsCache, probe_identity
from backend.app.services.filament_routing import resolve_filament_routing
from backend.app.services.print_option_defaults import preference_options
from backend.app.services.printer_manager import printer_manager
from backend.app.services.printer_occupancy import (
    PrinterOccupancyConflict,
    read_queue_occupancy,
    require_auto_placement,
)
from backend.app.services.queue_counters import update_queue_counters
from backend.app.services.queue_ops import queue_claim_scope
from backend.app.services.queue_rebalance import REBALANCE_SETTING_KEY
from backend.app.services.source_io import SOURCE_FAILURES, SourceUnavailable

logger = logging.getLogger(__name__)


SJF_SETTING_KEY = "queue_shortest_first"
PREFER_LOWEST_SETTING_KEY = "prefer_lowest_filament"


class AutoPlacementConflict(RuntimeError):
    """An expected auto-item race or stale-routing refusal.

    Unlike :class:`PrinterOccupancyConflict`, this can describe the router
    item or its evidence rather than the selected printer lane.  Callers must
    handle it as a normal retry/wait outcome, never as a 500.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _feed_moved(printer_id: int, policy, plan, signature: tuple[int, str] | None) -> bool:
    """Has the feed moved since this plan was resolved — asked under its own policy?

    ⚠️ Not ``plan.snapshot_marker``. That is the RAW revision, and it hashes
    every tray's ``tray_info_idx``: re-profiling a spool between the routing pass
    and this assignment moved it, so a job that said «any ABS will do» lost its
    placement to a fact its own plan had been told to ignore — and the tick
    reported it with a full ``logger.exception`` stack trace, which reads like a
    bug because it looks like one.

    ``signature`` is :func:`feed_signature` taken at plan time, when the policy
    and that snapshot were both in hand (``EligiblePrinter.snapshot_signature``).
    Without one — a caller that handed a plan over on its own — the raw marker is
    the baseline, which is exactly what ``feed_signature`` answers for a policy
    that keeps the profile.
    """
    current = printer_manager.get_feed_snapshot(printer_id)
    if signature is None:
        return current.marker != plan.snapshot_marker
    return feed_signature(policy, current) != signature


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
            prefer_lowest = await _get_bool_setting(db, PREFER_LOWEST_SETTING_KEY, default=True)

            # 1. Busy set — see ``busy_printer_ids`` for why "any pending row" is part of it.
            busy_printers = await busy_printer_ids(db)

            # 2. Fetch pending auto items in scheduling order
            pending = await self._fetch_pending(db, sjf)
            items = list(pending)
            if not items:
                return

            logger.debug("AutoQueueScheduler: %d pending items, busy_printers=%s", len(items), busy_printers)

            # 3. Iterate and assign
            placed = 0
            changed_queue_printer_ids: set[int] = set()
            blocked: list[str] = []
            # The first item that could not be placed, kept to name *something*
            # concrete in the notification below — "4 jobs are waiting" is far
            # less useful than the name of one of them plus the reason.
            first_blocked: tuple[AutoQueueItem, str] | None = None
            # At most one printer is woken per pass — see _wake_offline_printer.
            woke_one = False
            requirements_cache = PrintRequirementsCache()
            for item in items:
                while True:
                    eligible = await find_eligible_printer(
                        db, item, busy_printers, cache=requirements_cache, prefer_lowest=prefer_lowest
                    )
                    printer, reason = eligible
                    if printer is None:
                        source_reason = getattr(eligible.requirements, "reason", None)
                        if source_reason in SOURCE_FAILURES:
                            await fail_auto_source(db, item, source_reason)
                            logger.warning("Auto item %s failed: %s", item.id, source_reason)
                            break
                        if not woke_one:
                            woke_one = await self._wake_offline_printer(db, item, busy_printers)
                        if reason and item.waiting_reason != reason:
                            logger.info("Auto item %s not placed: %s", item.id, reason)
                            item.waiting_reason = reason
                        blocked.append(reason or "no reason reported")
                        if first_blocked is None:
                            first_blocked = (item, reason or "no reason reported")
                        break

                    try:
                        await self._assign(
                            db,
                            item,
                            printer,
                            prefer_lowest=prefer_lowest,
                            plan=eligible.plan,
                            requirements=eligible.requirements,
                            snapshot_signature=eligible.snapshot_signature,
                        )
                    except SourceUnavailable as exc:
                        await db.refresh(item)
                        if exc.reason in SOURCE_FAILURES:
                            await fail_auto_source(db, item, exc.reason)
                        break
                    except (PrinterOccupancyConflict, AutoPlacementConflict) as exc:
                        # The candidate was lost after its snapshot. Exclude it
                        # and resolve the next one in this same bounded pass. A
                        # concurrently cancelled/assigned router row is not a
                        # printer conflict; leave it to its winning writer.
                        await db.refresh(item)
                        if isinstance(exc, AutoPlacementConflict) and exc.code == "item_changed":
                            break
                        busy_printers.add(printer.id)
                        logger.info(
                            "Auto item %s lost printer %s (%s); trying another candidate", item.id, printer.id, exc.code
                        )
                        continue
                    except Exception:
                        logger.exception("Failed to assign auto item %s to printer %s", item.id, printer.id)
                        break

                    busy_printers.add(printer.id)
                    placed += 1
                    changed_queue_printer_ids.add(printer.id)
                    if sjf:
                        await self._mark_jumped_peers(db, item)
                    break

            announce = self._log_stall(
                [item for item in items if item.status == "pending"], placed, blocked, busy_printers
            )

            # Cross-model rebalancing (spec 2026-09-10), behind its setting and only
            # when this pass left something pending. It converts and creates ROUTER
            # rows — the next tick places them like any other; nothing starts here.
            if placed < len(items) and await _get_bool_setting(db, REBALANCE_SETTING_KEY):
                try:
                    moved = await queue_rebalance.rebalance(db, busy_printers=busy_printers)
                except Exception:
                    logger.exception("AutoQueueScheduler: rebalancing failed")
                    # ⚠️ The hook commits inside itself (it releases the transaction
                    # before every file copy), so a failure that WAS a commit leaves
                    # this session in pending-rollback — and the ``db.commit()`` below
                    # would then raise a second, unrelated ``PendingRollbackError``
                    # as "AutoQueueScheduler tick failed", burying the real cause in
                    # a support bundle. The cause is already in the log line above;
                    # this only makes the session usable again. Nothing of the
                    # placement pass is lost: the hook's own commits made it durable.
                    await db.rollback()
                else:
                    if moved.converted:
                        logger.info(
                            "AutoQueueScheduler: rebalanced %d item(s), created %d, %d part(s) moved",
                            moved.converted,
                            moved.created,
                            moved.moved_parts,
                        )

            await db.commit()

            # After the commit: the notification is a side effect on the outside
            # world, and it should never be sent describing a state that then
            # failed to persist.
            if announce and first_blocked is not None:
                await self._notify_stall(db, *first_blocked)

            # The new PrintQueueItem is durable now. Tell open Queue pages to
            # refetch the one affected printer immediately instead of waiting
            # for their independent 15/30-second polling clocks.
            for printer_id in changed_queue_printer_ids:
                try:
                    await ws_manager.send_queue_changed(printer_id)
                except Exception:
                    # A WebSocket failure must never make a durable placement
                    # look like a failed scheduler tick; REST polling remains
                    # the fallback for a disconnected browser.
                    logger.exception("Failed to announce queue change for printer %s", printer_id)

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
        *,
        plan=None,
        requirements=None,
        snapshot_signature=None,
    ) -> PrintQueueItem:
        """Copy auto item into the printer's print_queue and mark assigned.

        The routing plan decides the mapping — resolved by the caller (the tick's
        eligibility pass) or here, and revalidated below before the row is
        claimed. ``snapshot_signature`` travels with a plan the caller resolved:
        see :func:`_feed_moved` for why the plan's own marker is not that answer.
        """
        async with db.begin_nested():
            policy = auto_policy(item)
            requirements = requirements or await read_item_requirements(db, item)
            if requirements.reason in SOURCE_FAILURES:
                raise SourceUnavailable(requirements.reason)
            if plan is None:
                snapshot = printer_manager.get_feed_snapshot(printer.id)
                plan = resolve_filament_routing(requirements, policy, snapshot, prefer_lowest=prefer_lowest).plan
                snapshot_signature = feed_signature(policy, snapshot)
            if plan is None:
                raise AutoPlacementConflict("routing_unavailable")
            ams_mapping_json = json.dumps(plan.mapping)

            # The auto queue has no one physical printer to configure.  Resolve
            # options only now, for the printer that actually won the routing
            # decision.  In particular this selects the originating operator's
            # P1S event macros rather than whatever happened to be visible when
            # the file was put into a mixed-model auto queue.
            owner = await db.get(User, item.created_by_id) if item.created_by_id is not None else None
            profile = await preference_options(db, owner, printer.model)
            options = profile.for_printer_queue() if profile else {}

            # A queued row must retain the full calibration mode, unlike the
            # model-agnostic auto row which only has bool mirrors.
            bed_mode = normalize_mode(options.get("bed_levelling", True))
            flow_mode = normalize_mode(options.get("flow_cali", True))
            nozzle_mode = normalize_mode(options.get("nozzle_offset_cali", True))

            # Swap macros are an exception to the profile: their applicability
            # depends on the chosen printer and the source bytes.  If source
            # metadata has gone away, fail closed rather than double-firing a
            # baked-in plate-change sequence.
            source_has_baked_swap_macros = await self._source_has_baked_swap_macros(db, item)
            execute_swap_macros = bool(options.get("execute_swap_macros", False))
            if not printer.swap_mode_enabled or source_has_baked_swap_macros:
                execute_swap_macros = False
            swap_macro_events = options.get("swap_macro_events") if execute_swap_macros else None

            # Revalidate the SAME plan after DB awaits and before claiming the row.
            # ⚠️ Both re-probes below carry ``identity.sha256``, so they ask the same
            # question the first read asked. For a captured source the identity is
            # hash-anchored; probing the same path without the label would produce a
            # stat-anchored identity that can never compare equal to it, and EVERY
            # snapshot-backed assignment would die on "evidence changed".
            identity = requirements.source_identity
            current_identity = await probe_identity(identity)
            if (
                _feed_moved(printer.id, policy, plan, snapshot_signature)
                or policy.fingerprint != plan.policy_fingerprint
                or identity != current_identity
            ):
                raise AutoPlacementConflict("routing_changed")
            # The re-probe below is small
            # metadata I/O only; queue-source capture already happened before
            # Auto Queue reached this assignment transaction.
            async with queue_claim_scope(db, printer.id):
                occupancy = await read_queue_occupancy(db, printer.id, for_update=True)
                printer_queue = occupancy.queue
                require_auto_placement(occupancy)
                # 3. Compute next position in the per-printer queue.
                max_pos = await db.scalar(
                    select(func.coalesce(func.max(PrintQueueItem.position), 0)).where(
                        PrintQueueItem.queue_id == printer_queue.id
                    )
                )
                next_pos = (max_pos or 0) + 1
                claimed = await db.execute(
                    update(AutoQueueItem)
                    .where(
                        AutoQueueItem.id == item.id,
                        AutoQueueItem.status == "pending",
                        AutoQueueItem.cancelled_at.is_(None),
                    )
                    .values(status="assigned")
                )
                if not claimed.rowcount:
                    raise AutoPlacementConflict("item_changed")

                current_identity = await probe_identity(identity)
                if _feed_moved(printer.id, policy, plan, snapshot_signature) or identity != current_identity:
                    raise AutoPlacementConflict("routing_changed")

                # 4. Build the per-printer item with the target model's profile.
                new_item = PrintQueueItem(
                    queue_id=printer_queue.id,
                    archive_id=item.archive_id,
                    library_file_id=item.library_file_id,
                    # m173: the promoted row prints the bytes the router row already
                    # captured — never a new read of the original (queue-source-spool
                    # spec §7). Both rows then own the blob until the shared cleanup:
                    # the assignment is not a hand-off of the only reference, and this
                    # carry needs no storage guard because the router row is read live
                    # in this same transaction, so the blob cannot be released under
                    # it. The snapshot travels beside the id: it is what keeps the
                    # display name and the plate fallback with the job.
                    queue_source_id=item.queue_source_id,
                    source_snapshot=item.source_snapshot,
                    project_id=item.project_id,
                    project_line_id=item.project_line_id,
                    position=next_pos,
                    scheduled_time=item.scheduled_time,
                    manual_start=False,
                    # Carried onto the per-printer row so the gate is re-checked at
                    # dispatch: eligibility only proves the printer was clean at the
                    # moment of routing, and another print can fail in between.
                    require_previous_success=item.require_previous_success,
                    auto_off_after=item.auto_off_after,
                    ams_mapping=ams_mapping_json,
                    filament_routing=serialize_policy(
                        policy,
                        archive_id=item.archive_id,
                        library_file_id=item.library_file_id,
                        requirements=requirements,
                        printer_id=printer.id,
                        exact_model=True,
                        # The promoted row's intent names the blob it was written about,
                        # and the revision it stamps is that blob's HASH (the
                        # requirements above were read through the descriptor). Before
                        # routing v2 this stamped the copy's mtime, which a portable
                        # restore changes — and the reader had to look away for it.
                        queue_source_id=item.queue_source_id,
                    ),
                    nozzle_mapping=item.nozzle_mapping,
                    plate_id=plan.resolved_plate_id,
                    bed_levelling=bed_mode == "on",
                    bed_levelling_mode=bed_mode,
                    flow_cali=flow_mode == "on",
                    flow_cali_mode=flow_mode,
                    layer_inspect=bool(options.get("layer_inspect", False)),
                    timelapse=bool(options.get("timelapse", False)),
                    timelapse_storage=options.get("timelapse_storage"),
                    use_ams=plan.use_ams,
                    nozzle_offset_cali=nozzle_mode == "on",
                    nozzle_offset_cali_mode=nozzle_mode,
                    mesh_mode_fast_check=bool(options.get("mesh_mode_fast_check", True)),
                    gcode_injection=bool(options.get("gcode_injection", False)),
                    execute_swap_macros=execute_swap_macros,
                    swap_macro_events=json.dumps(swap_macro_events) if swap_macro_events else None,
                    selected_macro_ids=(
                        json.dumps(options["selected_macro_ids"]) if "selected_macro_ids" in options else None
                    ),
                    status="pending",
                    batch_id=item.batch_id,
                    created_by_id=item.created_by_id,
                    source_auto_item_id=item.id,
                )
                db.add(new_item)
                await db.flush()
                # Persist the same live counts ordinary enqueue maintains before
                # the tick commits and tells clients to refetch this queue.
                await update_queue_counters(db, printer_queue.id)

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

    async def _source_has_baked_swap_macros(self, db: AsyncSession, item: AutoQueueItem) -> bool:
        """Whether the source already supplies its own swap macro sequence.

        A captured source can outlive its archive or library row.  Without that
        metadata we cannot prove the source is safe to augment, so the only
        safe answer is to suppress our swap macros.
        """
        if item.archive_id is not None:
            value = await db.scalar(select(PrintArchive.swap_compatible).where(PrintArchive.id == item.archive_id))
            return bool(value) if value is not None else True
        if item.library_file_id is not None:
            value = await db.scalar(select(LibraryFile.swap_compatible).where(LibraryFile.id == item.library_file_id))
            return bool(value) if value is not None else True
        return True

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
