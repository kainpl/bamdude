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
from copy import deepcopy
from datetime import datetime, timezone
from time import monotonic

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import async_session
from backend.app.core.websocket import ws_manager
from backend.app.i18n import current_language, t
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.schemas.calibration_mode import normalize_mode
from backend.app.services import queue_rebalance, swap_compatibility
from backend.app.services.auto_queue_eligibility import (
    busy_printer_ids,
    find_eligible_printer,
    offline_candidates_for,
    router_order,
    router_view_conditions,
)
from backend.app.services.auto_queue_waiting import WaitingNotice, freeze, more_issues
from backend.app.services.filament_intake import (
    fail_auto_source,
    item_descriptor,
    read_item_requirements,
    routing_detail,
    snapshot_for_copy,
    swap_basis,
    swap_basis_holds,
)
from backend.app.services.filament_policy import auto_policy, serialize_policy
from backend.app.services.filament_preflight import feed_signature
from backend.app.services.filament_requirements import PrintRequirementsCache, probe_identity
from backend.app.services.filament_routing import resolve_filament_routing
from backend.app.services.offline_feed import OfflineFeedCache, offline_shortfall
from backend.app.services.order_print_options import override_options
from backend.app.services.print_option_defaults import preference_options
from backend.app.services.printer_location_service import load_tree, path_of
from backend.app.services.printer_manager import printer_manager
from backend.app.services.printer_occupancy import (
    PrinterOccupancyConflict,
    read_queue_occupancy,
    require_auto_placement,
)
from backend.app.services.queue_counters import update_queue_counters
from backend.app.services.queue_ops import lane_held_in_process, queue_claim_scope
from backend.app.services.queue_rebalance import REBALANCE_SETTING_KEY
from backend.app.services.source_io import SOURCE_FAILURES, SourceUnavailable
from backend.app.utils.model_compatibility import model_compatibility
from backend.app.utils.printer_models import normalize_model_name

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


def compose_options(profile, order_print_options, requirements_model: str | None, printer_model: str | None) -> dict:
    """The options a job is written with on a printer of ``printer_model``: the creator's saved profile
    for that model, then the order's explicit choices for the job's own model — validated for the
    printer (``ValueError`` when they do not fit it: ``incompatible_target``).

    One composition for ``_assign`` and for the rebalancer's forecast (Codex review r1, T-R1/T-R3):
    the preparation a job is dated with and the swap request a printer may refuse come from here.
    The profile is read by the caller through this module's ``preference_options``.
    """
    options = profile.for_printer_queue() if profile else {}
    options.update(override_options(order_print_options, requirements_model, printer_model))
    return options


class AutoQueueScheduler:
    """Background loop that routes AutoQueueItems to idle printers."""

    _check_interval = 30  # seconds — same cadence as PrintScheduler

    @staticmethod
    async def _a_plain_printer_may_take(db: AsyncSession, item: AutoQueueItem, refused: set[int], **search) -> bool:
        """Whether a printer without swap mode would print ``item`` as it is, busy or not: a refusal by policy on
        the swap printers does not close that candidate (swap-source-lifecycle spec r4 §8)."""
        swap_printers = set(
            (await db.execute(select(Printer.id).where(Printer.swap_mode_enabled.is_(True)))).scalars().all()
        )
        return (await find_eligible_printer(db, item, refused | swap_printers, **search)).printer is not None

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
        self._active_waiting_facts: set[tuple] = set()
        self._waiting_contributors: dict[int, tuple[tuple, tuple]] = {}
        self._notification_generation = 0
        self._wake_cooldowns: dict[int, float] = {}
        self._confirmed_wakes: dict[int, float] = {}

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
        self._notification_generation += 1
        self._active_waiting_facts.clear()
        self._waiting_contributors.clear()
        self._confirmed_wakes.clear()
        self._wake_cooldowns.clear()
        logger.info("Auto-queue scheduler stopped")

    async def tick(self) -> None:
        """Single iteration: assign pending auto items to eligible printers."""
        from backend.app.services.farm_startup_guard import farm_startup_guard

        if not farm_startup_guard.allowed():
            return
        notification_generation = self._notification_generation
        from backend.app.services.printer_manager import printer_manager

        for printer_id in tuple(self._confirmed_wakes):
            if printer_manager.is_connected(printer_id):
                self._confirmed_wakes.pop(printer_id, None)
        async with async_session() as db:
            sjf = await _get_bool_setting(db, SJF_SETTING_KEY)
            prefer_lowest = await _get_bool_setting(db, PREFER_LOWEST_SETTING_KEY, default=True)

            # 1. Busy set — see ``busy_printer_ids`` for why "any pending row" is part of it.
            busy_printers = await busy_printer_ids(db)
            from backend.app.services.ha_sensor_manager import ha_sensor_manager

            interlocked_printers = set(await ha_sensor_manager.blocked_printers(db))

            # 2. Fetch pending auto items in scheduling order
            pending = await self._fetch_pending(db, sjf)
            items = list(pending)
            if not items:
                await db.commit()
                if notification_generation == self._notification_generation:
                    self._active_waiting_facts.clear()
                    self._waiting_contributors.clear()
                    self._last_stall = None
                return

            logger.debug("AutoQueueScheduler: %d pending items, busy_printers=%s", len(items), busy_printers)

            # 3. Iterate and assign
            placed = 0
            changed_queue_printer_ids: set[int] = set()
            blocked: list[str] = []
            observations = {}
            complete = True
            exclusion_reasons: dict[int, str] = {}
            woken_printer_ids: set[int] = set()
            # At most one printer is woken per pass — see _wake_offline_printer.
            woke_one = False
            requirements_cache = PrintRequirementsCache()
            # What each switched-off printer holds, read once per pass: the
            # matcher's reason and the wake step ask about the same printers.
            offline_feeds = OfflineFeedCache()
            for item in items:
                # Candidates THIS item's swap request was refused on (D2): out of its own
                # search only -- the printers are not busy, and the next item may print there.
                swap_refused: set[int] = set()
                while True:
                    eligible = await find_eligible_printer(
                        db,
                        item,
                        busy_printers | swap_refused,
                        exclusion_reasons={**exclusion_reasons, **dict.fromkeys(swap_refused, "swap_request_refused")},
                        cache=requirements_cache,
                        prefer_lowest=prefer_lowest,
                        offline_feeds=offline_feeds,
                        interlocked_printers=interlocked_printers,
                    )
                    printer, reason = eligible
                    if (
                        printer is None
                        and swap_refused
                        and not await self._a_plain_printer_may_take(
                            db,
                            item,
                            swap_refused,
                            cache=requirements_cache,
                            prefer_lowest=prefer_lowest,
                            offline_feeds=offline_feeds,
                            interlocked_printers=interlocked_printers,
                        )
                    ):
                        # Every candidate refused the unknown source's request and no printer
                        # without swap mode could print it: a visible failure with its reason
                        # and Retry -- not a wait that would never end (swap-source-lifecycle
                        # spec r4 §8). With such a printer, busy now, the job waits for it below.
                        await fail_auto_source(db, item, swap_compatibility.UNKNOWN_SOURCE_REASON)
                        logger.info("Auto item %s failed: every candidate refused an unknown source", item.id)
                        break
                    if printer is None:
                        source_reason = getattr(eligible.requirements, "reason", None)
                        if source_reason in SOURCE_FAILURES:
                            await fail_auto_source(db, item, source_reason)
                            logger.warning("Auto item %s failed: %s", item.id, source_reason)
                            break
                        if not woke_one:
                            woke_one = await self._wake_offline_printer(
                                db,
                                item,
                                busy_printers,
                                requirements=eligible.requirements,
                                offline_feeds=offline_feeds,
                                woken_printer_ids=woken_printer_ids,
                            )
                        facts = self._waiting_facts(eligible.facts)
                        if facts != eligible.facts:
                            reason = " | ".join(f.message for f in facts)
                        if reason and item.waiting_reason != reason:
                            logger.info("Auto item %s not placed: %s", item.id, reason)
                            item.waiting_reason = reason
                        blocked.append(reason or "no reason reported")
                        observations[item.id] = (self._waiting_revision(item), facts)
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
                        else:
                            observations[item.id] = (self._waiting_revision(item), ())
                        break
                    except (PrinterOccupancyConflict, AutoPlacementConflict) as exc:
                        # The candidate was lost after its snapshot. Exclude it
                        # and resolve the next one in this same bounded pass. A
                        # concurrently cancelled/assigned router row is not a
                        # printer conflict; leave it to its winning writer.
                        await db.refresh(item)
                        if isinstance(exc, AutoPlacementConflict) and exc.code in (
                            "item_changed",
                            "swap_source_changed",
                        ):
                            break
                        if (
                            isinstance(exc, AutoPlacementConflict)
                            and exc.code == swap_compatibility.UNKNOWN_SOURCE_REASON
                        ):
                            swap_refused.add(printer.id)
                            logger.info(
                                "Auto item %s: printer %s refuses its unknown source's swap request",
                                item.id,
                                printer.id,
                            )
                            continue
                        busy_printers.add(printer.id)
                        exclusion_reasons[printer.id] = (
                            exc.code
                            if exc.code in {"incompatible_target", "compatible_fallback_disabled"}
                            else "placement_conflict"
                        )
                        logger.info(
                            "Auto item %s lost printer %s (%s); trying another candidate", item.id, printer.id, exc.code
                        )
                        continue
                    except Exception:
                        complete = False
                        logger.exception("Failed to assign auto item %s to printer %s", item.id, printer.id)
                        break

                    busy_printers.add(printer.id)
                    placed += 1
                    changed_queue_printer_ids.add(printer.id)
                    if sjf:
                        await self._mark_jumped_peers(db, item)
                    break

            self._log_stall([item for item in items if item.status == "pending"], placed, blocked, busy_printers)

            # Placement must be durable before an optional notification read.
            # In particular, a PostgreSQL read error poisons only this NEW
            # transaction, never the queue inserts of the placement pass.
            await db.commit()
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

            # Cross-model rebalancing (spec 2026-09-10), behind its setting and only
            # when this pass left something pending. It converts and creates ROUTER
            # rows — the next tick places them like any other; nothing starts here.
            if (
                notification_generation == self._notification_generation
                and placed < len(items)
                and await _get_bool_setting(db, REBALANCE_SETTING_KEY)
            ):
                try:
                    # The same busy set, interlock snapshot and parses the router
                    # pass just used: one HA read per tick, and readiness that
                    # cannot disagree with the ranking it mirrors (C3-1).
                    moved = await queue_rebalance.rebalance(
                        db,
                        busy_printers=busy_printers,
                        interlocked_printers=interlocked_printers,
                        requirements_cache=requirements_cache,
                    )
                except Exception:
                    complete = False
                    logger.exception("AutoQueueScheduler: rebalancing failed")
                    # ⚠️ The hook commits inside itself (it releases the transaction
                    # before every file copy), so a failure that WAS a commit leaves
                    # this session in pending-rollback — and the ``db.commit()`` below
                    # would then raise a second, unrelated ``PendingRollbackError``
                    # as "AutoQueueScheduler tick failed", burying the real cause in
                    # a support bundle. The cause is already in the log line above;
                    # this only makes the session usable again. The placement
                    # pass was already committed before optional rebalancing.
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
            if complete and notification_generation == self._notification_generation:
                try:
                    current = {}
                    contributors = {}
                    pending_now = await self._fetch_pending(db, sjf)
                    for item in pending_now:
                        observation = observations.get(item.id)
                        identity = self._waiting_revision(item)
                        if observation is None or observation[0] != identity:
                            current = None
                            break
                        facts = observation[1]
                        previous = self._waiting_contributors.get(item.id)
                        retained = previous[1] if previous and previous[0] == identity else ()
                        unknown_all = not facts or any(f.preserves_incident and f.printer_id is None for f in facts)
                        uncertain_printers = {f.printer_id for f in facts if f.preserves_incident}
                        confirmed = [f for f in facts if f.actionable]
                        confirmed.extend(f for f in retained if unknown_all or f.printer_id in uncertain_printers)
                        contributors[item.id] = (identity, tuple(dict.fromkeys(confirmed)))
                        for fact in contributors[item.id][1]:
                            key = fact.key(item.target_model, item.target_location_id)
                            current.setdefault(key, (item, fact))
                    notice = None
                    if current is not None:
                        new = set(current) - self._active_waiting_facts
                        if new:
                            notice = await self._waiting_notice(db, current, new)
                    # Close the read transaction before provider I/O. No ORM
                    # object is retained in the in-memory contributor ledger.
                    await db.commit()
                    if current is not None and notification_generation == self._notification_generation:
                        self._waiting_contributors = contributors
                        self._active_waiting_facts = set(current)
                        if notice is not None:
                            await self._notify_stall(db, notice)
                except Exception as exc:
                    await db.rollback()
                    logger.warning("AutoQueue notification projection failed error_type=%s", type(exc).__name__)

    def _log_stall(self, items: list, placed: int, blocked: list[str], busy_printers: set[int]) -> bool:
        """Say once, at INFO, that a tick could place nothing — and why.

        Without this the only trace of a stalled auto-queue was a DEBUG line
        nobody has enabled, so an INFO-level support bundle showed a healthy
        application and an idle farm with work waiting. Throttled on a signature
        of (reasons, busy printers) and re-stated every
        ``_stall_reminder_seconds`` so a long stall stays visible to whoever
        collects the log later.

        The return value describes only log throttling. Notification admission
        independently compares committed structured facts; neither this reminder
        nor a neighbouring placement resets that state.
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

    @staticmethod
    def _waiting_revision(item):
        # Only a same-pass freshness check, not incident identity. Names, row
        # ids and revisions never enter WaitingFact.key().
        ignored = {"waiting_reason", "position", "been_jumped", "created_at"}
        return tuple(
            (column.name, freeze(getattr(item, column.name)))
            for column in AutoQueueItem.__table__.columns
            if column.name not in ignored
        )

    async def _waiting_notice(self, db, current, new):
        ordered = sorted(new, key=repr)
        representative = current[ordered[0]][0]
        language = current_language()

        def model_name(item):
            return (
                normalize_model_name(item.target_model)
                or item.target_model
                or t(language, "filament_routing", "autoqueue_any_model")
            )

        models = sorted({model_name(current[key][0]) for key in ordered})
        tree = await load_tree(db) if any(current[key][0].target_location_id is not None for key in ordered[:5]) else {}
        lines = []
        for key in ordered[:5]:
            item, fact = current[key]
            context = model_name(item)
            if item.target_location_id is not None:
                context += " / " + (
                    path_of(tree, item.target_location_id)
                    if item.target_location_id in tree
                    else t(language, "filament_routing", "autoqueue_location_unavailable")
                )
            # One bounded digest, not an unbounded series of provider chunks.
            lines.append(f"{context}: {fact.message}"[:320])
        if len(ordered) > 5:
            lines.append(more_issues(language, len(ordered) - 5))
        target = (
            models[0] if len(models) == 1 else t(current_language(), "filament_routing", "autoqueue_multiple_models")
        )
        return WaitingNotice((await self._job_name(db, representative))[:160], target, "\n".join(lines))

    async def _notify_stall(self, db: AsyncSession, notice: WaitingNotice) -> None:
        """One committed scalar digest through existing provider/per-chat policy."""
        from backend.app.services.notification_service import notification_service

        try:
            await notification_service.on_queue_job_waiting(
                job_name=notice.job_name,
                target_model=notice.target_model,
                waiting_reason=notice.waiting_reason,
                db=db,
            )
        except Exception:
            logger.exception("Failed to send the queue_job_waiting notification")

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
                  print_time_seconds ASC NULLS LAST, position, id``
        SJF off: ``ORDER BY position, id``

        Which rows and in what order are ``auto_queue_eligibility.router_view_conditions`` /
        ``router_order`` — the rebalancer's forecast replays exactly this.
        """
        now = datetime.now(timezone.utc)
        stmt = select(AutoQueueItem).where(*router_view_conditions(now)).order_by(*router_order(sjf))
        result = await db.execute(stmt.execution_options(populate_existing=True))
        return result.scalars().all()

    # printer_id -> monotonic deadline before which we will not try to wake it
    # again. Attempt cooldown and confirmed boot grace are separate evidence.
    _WAKE_COOLDOWN_SECONDS = 600.0

    @staticmethod
    def _wake_now():
        return monotonic()

    def _waiting_facts(self, facts):
        from dataclasses import replace

        now = self._wake_now()
        return tuple(
            replace(
                f,
                code="printer_waking",
                params=(),
                message=f"{f.printer_name}: {routing_detail('printer_waking')['message']}"
                if f.printer_name
                else routing_detail("printer_waking")["message"],
            )
            if f.code == "printer_offline" and self._confirmed_wakes.get(f.printer_id, 0) > now
            else f
            for f in facts
        )

    async def _wake_offline_printer(
        self, db, item, busy_printers: set[int], *, requirements=None, offline_feeds=None, woken_printer_ids=None
    ) -> bool:
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

        ⚠️ **Attempt cooldown is not successful-wake evidence.** Both outcomes
        prevent repeated ON attempts for one cooldown. Only every configured
        auto-on plug acknowledging ON grants the separate bounded boot grace.
        Repeating ON while the printer stays offline never extends that grace;
        a positive connection retires the episode. We never wait for boot here.

        ⚠️ **A printer we failed to wake is NOT added to ``busy_printers``.** It
        is off, not busy — labelling it busy would misdescribe it in every later
        item's waiting reason, and an all-busy reason is treated as needing no
        user action, so it would suppress the notification too.

        ⚠️ **A printer that cannot run the job is passed over** (upstream #2876):
        its assigned inventory and last reading say what it holds while it is
        off (``offline_feed``), and one whose every holder is known and lacks a
        channel's material or forced colour is not the printer to switch on.
        Unknown is woken — never having heard is not the same as nothing loaded.
        """
        from backend.app.models.smart_plug import SmartPlug
        from backend.app.services.smart_plug_manager import smart_plug_manager

        candidates = await offline_candidates_for(db, item, busy_printers)
        if not candidates:
            return False

        now = self._wake_now()
        for printer in candidates:
            # Expire on read, so a live entry can never be overwritten by a
            # later success and a stale one costs nothing.
            deadline = self._wake_cooldowns.get(printer.id)
            if deadline is not None and deadline > now:
                continue

            if requirements is not None and offline_feeds is not None:
                missing = offline_shortfall(requirements, auto_policy(item), await offline_feeds.get(db, printer.id))
                if missing:
                    logger.info(
                        "Auto item %s: not powering on %s — what it holds cannot run the job (needs %s)",
                        item.id,
                        printer.name,
                        ", ".join(m["wanted"] for m in missing),
                    )
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
            succeeded = True
            for plug in plugs:
                from backend.app.services.farm_startup_guard import farm_startup_guard

                if not farm_startup_guard.allowed():
                    return True
                try:
                    service = await smart_plug_manager.get_service_for_plug(plug, db)
                    if not await service.turn_on(plug):
                        succeeded = False
                except Exception:
                    succeeded = False
                    logger.exception("Failed to power on plug %s for auto item %s", plug.id, item.id)
            if succeeded:
                # Never extend the deadline while a printer remains offline.
                # Only an observed connection retires this boot episode.
                self._confirmed_wakes.setdefault(printer.id, now + self._WAKE_COOLDOWN_SECONDS)
                if woken_printer_ids is not None:
                    woken_printer_ids.add(printer.id)
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
            verdict = model_compatibility(
                requirements.model, printer_manager.effective_model_for(printer.id, printer.model)
            )
            if verdict not in ("exact", "compatible"):
                raise AutoPlacementConflict("incompatible_model")
            target_verdict = model_compatibility(
                item.target_model, printer_manager.effective_model_for(printer.id, printer.model)
            )
            if target_verdict == "incompatible":
                raise AutoPlacementConflict("incompatible_target")
            if target_verdict == "compatible" and not await _get_bool_setting(db, "auto_queue_compatible_models"):
                raise AutoPlacementConflict("compatible_fallback_disabled")
            if plan is None:
                snapshot = printer_manager.get_feed_snapshot(printer.id)
                plan = resolve_filament_routing(
                    requirements,
                    policy,
                    snapshot,
                    exact_model=verdict == "exact",
                    prefer_lowest=prefer_lowest,
                ).plan
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
            try:
                options = compose_options(profile, item.order_print_options, requirements.model, printer.model)
            except ValueError as exc:
                logger.info("Auto item %s: explicit print options do not fit target model %s", item.id, printer.model)
                raise AutoPlacementConflict("incompatible_target") from exc

            # A queued row must retain the full calibration mode, unlike the
            # model-agnostic auto row which only has bool mirrors.
            bed_mode = normalize_mode(options.get("bed_levelling", True))
            flow_mode = normalize_mode(options.get("flow_cali", True))
            nozzle_mode = normalize_mode(options.get("nozzle_offset_cali", True))

            # Swap macros are an exception to the profile: their applicability
            # depends on the chosen printer and the source bytes -- the job's OWN
            # captured declaration, never its library or archive row, which may be
            # gone or re-declared, and never this router row's inert swap fields
            # (swap-source-lifecycle spec r4 §8). An unknown source this printer's
            # profile asks our macros for refuses THIS candidate; the tick tries the
            # next one.
            descriptor = await item_descriptor(db, item)
            # What this decision is taken from: re-read under the claim below, and the assignment is abandoned
            # when a refresh or a source declaration moved it in between (swap-source-declaration spec r3 §6).
            basis = swap_basis(item, descriptor)
            try:
                execute_swap_macros, swap_macro_events = swap_compatibility.resolve_request(
                    descriptor.swap_fact.value if descriptor is not None else None,
                    execute=options.get("execute_swap_macros", False),
                    events=options.get("swap_macro_events"),
                    printer_swap_mode=bool(printer.swap_mode_enabled),
                )
            except swap_compatibility.SwapRequestRefused as exc:
                raise AutoPlacementConflict(exc.reason) from exc

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
            # The promoted row records the router row's declaration in a dict of its own -- the very fact the
            # swap fields above were decided from, never a second read.
            promoted_snapshot = await snapshot_for_copy(db, item, descriptor)
            # The tick's transaction may hold SQLite's writer already; a lane another writer holds in process is
            # this printer lost for the pass, never a wait (queue_ops.lane_held_in_process).
            if lane_held_in_process(db, printer.id):
                raise AutoPlacementConflict("lane_busy")
            async with queue_claim_scope(db, printer.id):
                from backend.app.services.order_resources import allowed

                if not await allowed(db, item.project_id, printer.id, lock=True, project_line_id=item.project_line_id):
                    raise AutoPlacementConflict("item_changed")
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
                # Under the claim: the row, its blob and its source's fact read fresh, compared with what the swap
                # decision and the promoted snapshot came from. A difference is no assignment; the next pass
                # decides everything from the new state.
                if not await swap_basis_holds(db, item, basis):
                    raise AutoPlacementConflict("swap_source_changed")

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
                    source_snapshot=promoted_snapshot,
                    auto_eject=item.auto_eject,
                    auto_eject_settings=deepcopy(item.auto_eject_settings),
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
                        exact_model=verdict == "exact",
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
                    preheat_override=options.get("preheat_override", "inherit"),
                    preheat_chamber_target_override=options.get("preheat_chamber_target_override"),
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
