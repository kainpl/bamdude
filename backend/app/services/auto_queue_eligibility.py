"""Eligibility evaluation for auto-queue items.

Given an AutoQueueItem and a set of printers already busy in the
current scheduler tick, pick the printer whose queue the item should join:

1. Matches ``target_model`` (case-insensitive, normalised).
2. Matches ``target_location`` if specified.
3. Has ``auto_distribute_eligible=True`` on its PrinterQueue.
4. Is connected over MQTT.
5. Can feed every used channel of the plate: the routing plan is resolved
   against the printer's feed under the item's own policy
   (``resolve_filament_routing``), so what counts as a match — base
   material or the exact profile, exact colour or not — is the question the
   item was queued with, and the same one dispatch asks again.
6. Satisfies ``filament_overrides``: when an override has
   ``force_color_match=True``, the printer must have an exact type+color
   match in some loaded slot — and the same ``tray_info_idx`` when both
   sides carry one, so PLA Basic/Matte/Silk are not interchangeable.
   Without the flag, color matches are counted as a preference and the
   highest-scoring printer wins.

**Routing is not dispatching, and readiness is not a filter here.** Whether a
printer can start *right now* — plate-clear gate, drying, staggering, the lot —
is decided by ``print_scheduler.check_queue`` at dispatch, from the DB claim on
``PrinterQueue.status`` and the live printer state. Asking the same question a
second time at routing time does not make anything safer: an item placed in a
blocked printer's queue simply waits there, visibly, until that printer is
ready. What it *did* do was refuse to place anything at all, which is how an
operator ended up with three idle machines, an auto-queue reporting
"Busy: A1M-TR, A1M-TL, A1M-BL", and no Clear Plate prompt anywhere — that
prompt renders off the printer's own queue, which auto-queue was declining to
fill. Readiness now only ranks candidates (see the sort in
``find_eligible_printer``); a ready printer wins, a busy one still gets work.

This diverges from upstream ``PrintScheduler._find_idle_printer_for_model``,
which has one flat queue and therefore no "place it and let the owner decide"
option. PrinterQueue also carries the ``auto_distribute_eligible`` opt-out flag.

Returns a tuple ``(printer, waiting_reason)``:
- ``(Printer, None)`` if eligible
- ``(None, reason_string)`` describing why no printer is available
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.i18n import current_language, t
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.settings import Settings
from backend.app.services.auto_queue_waiting import (
    KNOWN_REASONS,
    WaitingFact,
    fact,
    file_params,
    offline_params,
    routing_params,
)
from backend.app.services.filament_intake import read_item_requirements, routing_detail
from backend.app.services.filament_policy import auto_policy
from backend.app.services.filament_preflight import feed_signature
from backend.app.services.filament_requirements import PrintRequirementsCache
from backend.app.services.filament_routing import resolve_filament_routing
from backend.app.services.offline_feed import offline_shortfall
from backend.app.services.print_scheduler import scheduler
from backend.app.services.printer_location_service import load_tree, path_of, subtree_ids
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.model_compatibility import model_compatibility
from backend.app.utils.printer_models import normalize_model_name

logger = logging.getLogger(__name__)


def router_view_conditions(now) -> tuple:
    """The rows the router deals out on a pass (``AutoQueueScheduler._fetch_pending``): pending,
    not cancelled, not staged for a manual start, not scheduled for later. The rebalancer's
    forecast reads exactly these as the tail ahead of and beside its candidates (spec
    rebalance-tail-time T6) — one definition, so the two cannot drift."""
    return (
        AutoQueueItem.status == "pending",
        AutoQueueItem.cancelled_at.is_(None),
        AutoQueueItem.manual_start.is_(False),
        or_(AutoQueueItem.scheduled_time.is_(None), AutoQueueItem.scheduled_time <= now),
    )


def router_order(sjf: bool) -> tuple:
    """The router's order. SJF: the model column, ``been_jumped`` DESC, the time with no estimate
    last, the position; otherwise the position. The id is the last key on both — ties between
    rows were undefined before (heap order on PostgreSQL), and the rebalancer's replay
    (``rebalance_replay.router_key``) breaks them the same way (spec T8)."""
    if sjf:
        return (
            AutoQueueItem.target_model,
            AutoQueueItem.been_jumped.desc(),
            AutoQueueItem.print_time_seconds.asc().nullslast(),
            AutoQueueItem.position,
            AutoQueueItem.id,
        )
    return (AutoQueueItem.position, AutoQueueItem.id)


async def busy_printer_ids(db: AsyncSession) -> set[int]:
    """The printers the router may not place on this tick.

    A printer is off-limits when EITHER its queue is printing OR its queue
    already holds a pending item, however that item got there — manual queue,
    scheduled, a prior auto-route. The ``status='printing'`` clause alone is not
    enough: between auto-queue tick N (which assigns items 1..K to K printers as
    pending rows) and the per-printer scheduler's next tick (which flips
    ``PrinterQueue.status`` as its synchronous prep walks the items in queue_id
    order) there is a window where some printers have flipped and the lagging
    ones have not. A tick that fires inside it sees the laggards as free and
    double-stacks the next items onto them — every new auto item landing on the
    same lagging printer. "Has any pending row" closes the gap: each tick places
    at most one new item per printer, and the next placement waits until the
    queue actually drains.

    One function, so the rebalancer (``services/queue_rebalance.py``) reads the
    same definition the tick does.
    """
    from backend.app.services.printer_occupancy import active_claim_printer_ids

    printing = await active_claim_printer_ids(db)
    holding = await db.execute(
        select(PrinterQueue.printer_id)
        .join(PrintQueueItem, PrintQueueItem.queue_id == PrinterQueue.id)
        .where(PrintQueueItem.status == "pending")
        .distinct()
    )
    return printing | {pid for (pid,) in holding.all()}


async def printers_for_item(db: AsyncSession, item: AutoQueueItem) -> tuple[list[Printer], str, str]:
    """Every printer this item is allowed to run on, before any readiness is asked.

    Returns ``(printers, normalized_model, location_suffix)``.

    ⚠️ **One source for "which printers can this job run on".** The matcher asks
    it to rank candidates; :func:`offline_candidates_for` asks it to decide
    which printer may be woken. Two queries would eventually disagree, and the
    disagreement that matters is switching a printer on for a file that can
    never legally run there — the job stays stuck and the printer now draws
    power.
    """
    # ⚠️ ``normalize_model_name``, not ``normalize_printer_model``: the latter
    # hands an internal code straight back, so an item targeting "C12" matched
    # no printer row and waited for ever behind "No active C12 printers
    # eligible". Normalising HERE covers every creator — the route, telegram,
    # the virtual printer — rather than each of them separately.
    normalized_model = normalize_model_name(item.target_model) or item.target_model

    # Filter active printers of the right model + location, with auto-distribute eligible.
    query = (
        select(Printer)
        .join(PrinterQueue, PrinterQueue.printer_id == Printer.id)
        .where(Printer.is_active.is_(True))
        .where(Printer.archived.is_(False))
        .where(PrinterQueue.auto_distribute_eligible.is_(True))
        # An operator-paused queue refuses new work — auto-queue included.
        .where(PrinterQueue.is_paused.is_(False))
    )
    location_suffix = ""
    if item.target_location_id:
        # The SUBTREE, by id. Aiming work at a workshop has to reach the
        # printers on its shelves — before this the item had to name each shelf.
        # By id and not by name because the string comparison this replaces made
        # "Цех 2" and "цех 2" two different places, so an item aimed at a
        # mistyped one matched nothing, silently and for ever.
        #
        # A GATE, not a rank: a printer standing directly on the workshop gets
        # no preference over one on a shelf. "Who is ready" is the dispatcher's
        # question and it already ranks.
        tree = await load_tree(db)
        query = query.where(Printer.location_id.in_(subtree_ids(tree, item.target_location_id)))
        # Resolved for the message only. An operator reading "why did nothing
        # move" is not helped by a row id — and with a tree, not by a bare name
        # either: "no printers in Shelf" reads oddly when a workshop was chosen.
        if item.target_location_id in tree:
            location_suffix = t(
                current_language(),
                "filament_routing",
                "autoqueue_location_suffix",
                path=path_of(tree, item.target_location_id),
            )

    result = await db.execute(query)
    from backend.app.services.order_resources import permits, policy_for

    resources = await policy_for(db, item.project_id, project_line_id=item.project_line_id)
    setting = await db.scalar(select(Settings.value).where(Settings.key == "auto_queue_compatible_models"))
    allow_compatible = isinstance(setting, str) and setting.lower() == "true"
    exact, compatible = [], []
    for printer in result.scalars().all():
        if not permits(resources, printer.id, printer_manager.effective_model_for(printer.id, printer.model)):
            continue
        verdict = model_compatibility(normalized_model, printer_manager.effective_model_for(printer.id, printer.model))
        if verdict == "exact":
            exact.append(printer)
        elif verdict == "compatible" and allow_compatible:
            compatible.append(printer)
    printers = exact + compatible
    return printers, normalized_model, location_suffix


async def offline_candidates_for(db: AsyncSession, item: AutoQueueItem, busy_printers: set[int]) -> list[Printer]:
    """Printers this item could run on that are simply switched off.

    ⚠️ **Being disconnected is the one readiness question the matcher uses as a
    gate**, and it has to: routing matches the filament actually loaded, which
    is live MQTT state, and a printer that is off reports none. So the gate
    stays — what was missing is this: when nothing is eligible *because* the
    candidates are off, somebody has to switch one on, or the item waits for
    ever while the identical job pinned to a printer wakes it in one pass.

    ⚠️ A printer **awaiting plate-clear acknowledgement is excluded**. Waking it
    buys nothing: it boots into IDLE and is held by that gate anyway. The flag
    is ours and persisted, so it is readable while the printer is still off.
    """
    printers, _model, _suffix = await printers_for_item(db, item)
    from backend.app.services.printer_manager import printer_manager as _pm

    return [
        p
        for p in printers
        if p.id not in busy_printers and not _pm.is_connected(p.id) and not _pm.is_awaiting_plate_clear(p.id)
    ]


@dataclass(frozen=True)
class EligiblePrinter:
    printer: Printer | None = None
    reason: str | None = None
    plan: object = None
    requirements: object = None
    #: What "the feed has not moved" meant when this plan was resolved, read
    #: under the item's own policy — the auto-queue placement's own "feed moved"
    #: baseline (``auto_queue_scheduler._feed_moved``). The plan's own
    #: ``snapshot_marker`` cannot stand in for it: that is the raw revision, and
    #: it moves on a profile retag a job with «allow base material match» was
    #: told to ignore.
    snapshot_signature: tuple[int, str] | None = None
    facts: tuple[WaitingFact, ...] = ()

    def __iter__(self):
        # Compatibility for callers that only display the result. Assignment
        # consumers carry plan and requirements, never a second greedy mapping.
        return iter((self.printer, self.reason))


async def find_eligible_printer(
    db: AsyncSession,
    item: AutoQueueItem,
    busy_printers: set[int],
    require_plate_clear: bool = True,
    *,
    cache: PrintRequirementsCache | None = None,
    prefer_lowest: bool = False,
    offline_feeds=None,
    interlocked_printers: set[int] | None = None,
    exclusion_reasons: dict[int, str] | None = None,
) -> EligiblePrinter:
    """The best printer for ``item`` now, or why there is none.

    ``offline_feeds`` (an ``offline_feed.OfflineFeedCache``) lets a switched-off
    printer be described by what it lacks instead of as "offline": it is the
    same answer the wake step acts on, so the reason says why nothing was
    switched on (upstream #2876). Without it an off printer reads as offline.
    """
    if item.target_model:
        printers, normalized_model, location_suffix = await printers_for_item(db, item)
        if not printers:
            reason = t(
                current_language(),
                "filament_routing",
                "autoqueue_no_eligible_printer",
                model=normalized_model,
                location=location_suffix,
            )
            return EligiblePrinter(reason=reason, facts=(fact("no_eligible_printer", message=reason),))
    req = await read_item_requirements(db, item, cache)
    if req.status != "ok":
        reason = routing_detail(req.reason if req.reason in KNOWN_REASONS else "routing_unclassified")["message"]
        return EligiblePrinter(
            reason=reason, requirements=req, facts=(fact(req.reason, params=file_params(item, req), message=reason),)
        )
    item.plate_id = req.resolved_plate_id
    if not item.target_model:
        item.target_model = req.model
    printers, normalized_model, location_suffix = await printers_for_item(db, item)
    if not printers:
        reason = t(
            current_language(),
            "filament_routing",
            "autoqueue_no_eligible_printer",
            model=normalized_model,
            location=location_suffix,
        )
        return EligiblePrinter(reason=reason, facts=(fact("no_eligible_printer", message=reason),))
    policy = auto_policy(item)
    candidates, facts = [], []

    def refuse(printer, code, message, params=None, *, uncertain=False):
        if code not in KNOWN_REASONS:
            message = routing_detail("routing_unclassified")["message"]
        facts.append(
            fact(
                code,
                printer.id,
                params=params,
                message=f"{printer.name}: {message}",
                printer_name=printer.name,
                uncertain=uncertain,
            )
        )

    for printer in printers:
        verdict = model_compatibility(req.model, printer_manager.effective_model_for(printer.id, printer.model))
        if verdict not in ("exact", "compatible"):
            refuse(printer, "model_mismatch", routing_detail("model_mismatch")["message"], {"file_model": req.model})
            continue
        if printer.id in busy_printers:
            code = (exclusion_reasons or {}).get(printer.id, "printer_busy")
            display_code = (
                "swap_source_unknown"
                if code == "swap_request_refused"
                else code
                if code in {"incompatible_target", "compatible_fallback_disabled"}
                else "printer_busy"
            )
            refuse(printer, code, routing_detail(display_code)["message"])
            continue
        if item.require_previous_success and not await scheduler.previous_print_succeeded(db, printer.id):
            refuse(printer, "previous_print_failed", routing_detail("previous_print_failed")["message"])
            continue
        # Bound, not inlined: the snapshot the plan was resolved against is what
        # the assignment's re-read is compared to, and only here are the policy
        # and that snapshot both in hand.
        snapshot = printer_manager.get_feed_snapshot(printer.id)
        if not snapshot.connected and offline_feeds is not None:
            feed = await offline_feeds.get(db, printer.id)
            missing = offline_shortfall(req, policy, feed)
            if missing:
                loaded = ", ".join(dict.fromkeys(s.material for s in feed.sources))
                refuse(
                    printer,
                    "printer_off_missing_filament",
                    routing_detail(
                        "printer_off_missing_filament",
                        slot=missing[0]["slot"],
                        wanted=missing[0]["wanted"],
                        loaded=loaded,
                    )["message"],
                    offline_params(req, policy, snapshot, missing, feed),
                )
                continue
        result = resolve_filament_routing(
            req, policy, snapshot, exact_model=verdict == "exact", prefer_lowest=prefer_lowest
        )
        if result.plan is None:
            # With the facts: this line names ONE printer, so its trays can be
            # listed. Without them a farm-wide refusal reads as 24 identical
            # sentences and the operator cannot tell which channel disagreed.
            refuse(
                printer,
                result.reason,
                routing_detail(result.reason, **result.params)["message"],
                routing_params(result.reason, req, policy, snapshot, result.slots),
                uncertain=result.status == "unknown" and result.reason != "printer_offline",
            )
            continue
        ready = scheduler._is_printer_idle(printer.id, require_plate_clear) and printer.id not in (
            interlocked_printers or ()
        )
        candidates.append(
            (ready, verdict == "exact", result.plan.color_matches, -printer.id, printer, result.plan, snapshot)
        )
    if candidates:
        _, _, _, _, printer, plan, snapshot = max(candidates, key=lambda c: c[:4])
        return EligiblePrinter(
            printer, plan=plan, requirements=req, snapshot_signature=feed_signature(policy, snapshot)
        )
    facts.sort(key=lambda item: (item.printer_id or 0, item.code, repr(item.params)))
    if not facts:
        facts.append(fact("routing_unclassified", message=routing_detail("routing_unclassified")["message"]))
    return EligiblePrinter(reason=" | ".join(item.message for item in facts), requirements=req, facts=tuple(facts))
