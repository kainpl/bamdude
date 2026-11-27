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

**The rank is one tuple, written once** in ``printer_occupancy.placement_rank``:
readiness, exact model, colour matches, no deferred rows, the operator's printer
order (``services/printer_pick_order.py``, C01), the remaining-filament level,
the lower id. The wake step orders the switched-off candidates by the same order.

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
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

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
    freeze,
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
from backend.app.services.printer_occupancy import active_claim_printer_ids, holding_condition, placement_rank
from backend.app.services.printer_pick_order import PickOrder, read_pick_order
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


#: What a router row's revision leaves out (G03 B, A17 step 3): the pass's own writes and what it orders by.
REVISION_IGNORED = frozenset({"waiting_reason", "position", "been_jumped", "created_at"})

#: The columns the eligibility back-fills in memory (A23): persisted by the row's placement or the pass's end batch.
BACKFILLED = ("plate_id", "target_model")


def router_row_revision(item) -> tuple:
    """A router row's revision: every column but :data:`REVISION_IGNORED`, as plain values (G03 B, A17 step 3).

    One function, read by the decision, the pass's writes and the notification projection -- a same-pass freshness
    check, never incident identity (names, row ids and revisions never enter ``WaitingFact.key()``)."""
    return tuple(
        (column.name, freeze(getattr(item, column.name)))
        for column in AutoQueueItem.__table__.columns
        if column.name not in REVISION_IGNORED
    )


@dataclass(frozen=True)
class RouterRowView:
    """What a pass decided about: the row's id, its incarnation and its revision, taken at the fetch, before any
    back-fill, as plain values (G03 B, A17 step 3). Every write of the pass is addressed by one."""

    id: int
    recovery_identity: str | None
    revision: tuple

    def value(self, name: str):
        return dict(self.revision).get(name)


def router_row_view(item) -> RouterRowView:
    return RouterRowView(item.id, item.recovery_identity, router_row_revision(item))


def backfills(view: RouterRowView, item) -> dict:
    """The eligibility's in-memory back-fills on ``item`` since ``view`` was taken (A23), as column values."""
    return {name: getattr(item, name) for name in BACKFILLED if freeze(getattr(item, name)) != view.value(name)}


def router_row_outcome(row, view: RouterRowView) -> str:
    """``written`` when ``row`` (read under its lock) is still the row ``view`` decided about; ``gone`` when it is
    missing, another incarnation (a delete + SQLite id reuse) or no longer pending; ``stale`` when its revision moved
    (a source refresh, an edit)."""
    if (
        row is None
        or row.recovery_identity != view.recovery_identity
        or row.status != "pending"
        or row.cancelled_at is not None
    ):
        return "gone"
    if router_row_revision(row) != view.revision:
        return "stale"
    return "written"


async def begin_router_write(db: AsyncSession) -> None:
    """Start a short write transaction of the router's own: whatever read transaction the session had ends first
    (nothing is held across a placement, A17), then on SQLite the writer, before the rows are read."""
    from sqlalchemy import text

    if db.in_transaction():
        await db.commit()
    if db.get_bind().dialect.name == "sqlite":
        await db.execute(text("BEGIN IMMEDIATE"))


async def write_router_rows(db: AsyncSession, writes: dict[int, tuple[RouterRowView, dict]]) -> dict[int, str]:
    """Write router rows only where they are still the rows their views decided about (G03 B, A17 step 3).

    ``writes`` maps an id to ``(view, values)``. One short transaction of its own, committed here: the rows locked
    ascending (``lock_auto_rows``), re-read, compared (:func:`router_row_outcome`), and only a match written -- a
    compare-and-set on the id, the view's incarnation, ``pending`` and not cancelled. Returns each id's outcome; a
    non-``written`` one is part of the caller's pass (its observation is dropped, the projection does not resolve).
    """
    from sqlalchemy import update

    from backend.app.services.queue_counters import lock_auto_rows

    if not writes:
        return {}
    await begin_router_write(db)
    rows = await lock_auto_rows(db, writes)
    outcomes: dict[int, str] = {}
    for item_id in sorted(writes):
        view, values = writes[item_id]
        outcome = router_row_outcome(rows.get(item_id), view)
        if outcome == "written" and values:
            result = await db.execute(
                update(AutoQueueItem)
                .where(
                    AutoQueueItem.id == item_id,
                    AutoQueueItem.recovery_identity == view.recovery_identity,
                    AutoQueueItem.status == "pending",
                    AutoQueueItem.cancelled_at.is_(None),
                )
                .values(**values)
            )
            if not result.rowcount:
                outcome = "gone"
        outcomes[item_id] = outcome
    await db.commit()
    return outcomes


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


async def busy_printer_ids(db: AsyncSession, now: datetime) -> set[int]:
    """Active claims and printers with pending work that would start on its own.

    Before the per-printer scheduler claims an automatic placement, its due,
    unstaged copy already holds the lane. This keeps consecutive ticks from
    stacking placements on a printer. Future or staged rows leave the lane
    open. The placement writer rechecks under the lane with a fresh clock.
    """
    printing = await active_claim_printer_ids(db)
    holding = await db.scalars(
        select(PrinterQueue.printer_id)
        .join(PrintQueueItem, PrintQueueItem.queue_id == PrinterQueue.id)
        .where(holding_condition(now))
        .distinct()
    )
    return printing | set(holding)


async def deferred_printer_ids(db: AsyncSession, now: datetime) -> set[int]:
    """Printers that should rank lower because they have staged or future work."""
    return set(
        await db.scalars(
            select(PrinterQueue.printer_id)
            .join(PrintQueueItem, PrintQueueItem.queue_id == PrinterQueue.id)
            .where(PrintQueueItem.status == "pending", ~holding_condition(now))
            .distinct()
        )
    )


#: A printer queue in one of these states dispatches nothing until the operator resumes it (after a failed or
#: cancelled print) — ``print_scheduler.check_queue`` skips it and ``require_scheduler_claim`` refuses it.
HALTED_QUEUE_STATUSES = ("paused", "error")


async def printers_for_item(
    db: AsyncSession,
    item: AutoQueueItem,
    *,
    halted: list[Printer] | None = None,
    headers: dict[int, str | None] | None = None,
) -> tuple[list[Printer], str, str]:
    """Every printer this item is allowed to run on, before any readiness is asked.

    Returns ``(printers, normalized_model, location_suffix)``.

    ⚠️ **One source for "which printers can this job run on".** The matcher asks
    it to rank candidates; :func:`offline_candidates_for` asks it to decide
    which printer may be woken. Two queries would eventually disagree, and the
    disagreement that matters is switching a printer on for a file that can
    never legally run there — the job stays stuck and the printer now draws
    power.

    ⚠️ **A halted queue gets no work** (owner, G03 E-3): a queue left ``paused`` or
    ``error`` by a cancelled or failed print is out, exactly like an
    operator-paused one. Placed there, the job would sit until somebody pressed
    Resume while an identical printer idled; left in AutoQueue, any printer of the
    model can take it. Not a readiness question — the queue refuses ALL work until
    resumed, the category ``is_paused`` already belongs to. A caller that names the
    halted printers in its waiting reason passes ``halted``: the printers that would
    otherwise qualify are appended to it, never returned.

    ``headers``, when given, receives each candidate printer's queue header incarnation (``recovery_identity``) from
    this very read (G03 B, A25): a placement lands only in the header its eligibility judged, never in a header
    created later under the same id.
    """
    # ⚠️ ``normalize_model_name``, not ``normalize_printer_model``: the latter
    # hands an internal code straight back, so an item targeting "C12" matched
    # no printer row and waited for ever behind "No active C12 printers
    # eligible". Normalising HERE covers every creator — the route, telegram,
    # the virtual printer — rather than each of them separately.
    normalized_model = normalize_model_name(item.target_model) or item.target_model

    # Filter active printers of the right model + location, with auto-distribute eligible.
    query = (
        select(Printer, PrinterQueue.status, PrinterQueue.is_paused, PrinterQueue.recovery_identity)
        .join(PrinterQueue, PrinterQueue.printer_id == Printer.id)
        .where(Printer.is_active.is_(True))
        .where(Printer.archived.is_(False))
        .where(PrinterQueue.auto_distribute_eligible.is_(True))
    )
    if halted is None:
        # NULL is no halt (as the scheduler's ``status in (...)`` reads it), never filtered out by NOT IN.
        query = query.where(
            PrinterQueue.is_paused.is_(False),
            or_(PrinterQueue.status.is_(None), PrinterQueue.status.not_in(HALTED_QUEUE_STATUSES)),
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

    # N62 D2: the job's AUTHOR'S printers — a team's job stays on the team's printers. A gate like
    # the location, not a rank; asked at every decision, so a changed group moves nothing already
    # placed but holds what is still waiting. No author (a Virtual Printer, a chat without a user)
    # is no person: every printer.
    from backend.app.core.printer_scope import author_scope

    scope = await author_scope(db, item.created_by_id)
    if scope is not None:
        query = query.where(Printer.id.in_(scope))
        location_suffix += t(current_language(), "filament_routing", "autoqueue_author_scope_suffix")

    result = await db.execute(query)
    from backend.app.services.order_resources import permits, policy_for

    resources = await policy_for(db, item.project_id, project_line_id=item.project_line_id)
    setting = await db.scalar(select(Settings.value).where(Settings.key == "auto_queue_compatible_models"))
    allow_compatible = isinstance(setting, str) and setting.lower() == "true"
    exact, compatible, stopped = [], [], []
    for printer, queue_status, is_paused, queue_identity in result.all():
        if headers is not None:
            headers[printer.id] = queue_identity
        if not permits(resources, printer.id, printer_manager.effective_model_for(printer.id, printer.model)):
            continue
        verdict = model_compatibility(normalized_model, printer_manager.effective_model_for(printer.id, printer.model))
        if verdict not in ("exact", "compatible") or (verdict == "compatible" and not allow_compatible):
            continue
        if is_paused or queue_status in HALTED_QUEUE_STATUSES:
            stopped.append(printer)
        elif verdict == "exact":
            exact.append(printer)
        else:
            compatible.append(printer)
    if halted is not None:
        halted.extend(stopped)
    printers = exact + compatible
    return printers, normalized_model, location_suffix


async def offline_candidates_for(
    db: AsyncSession, item: AutoQueueItem, busy_printers: set[int], *, pick_order: PickOrder | None = None
) -> list[Printer]:
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

    **In the printer order** (C01, upstream #3480 / #2915): exact-model printers before compatible
    ones, as ``printers_for_item`` lists them, and inside each group by ``pick_order``, then the
    lower id — the printer switched on is the one the router would have picked had it been on.
    The pass hands its order in; without one it is read when there is a choice to make.
    """
    printers, normalized_model, _suffix = await printers_for_item(db, item)
    from backend.app.services.printer_manager import printer_manager as _pm

    candidates = [
        p
        for p in printers
        if p.id not in busy_printers and not _pm.is_connected(p.id) and not _pm.is_awaiting_plate_clear(p.id)
    ]
    if len(candidates) > 1:
        if pick_order is None:
            pick_order = await read_pick_order(db, now=datetime.now(timezone.utc))
        exact = {
            p.id
            for p in candidates
            if model_compatibility(normalized_model, _pm.effective_model_for(p.id, p.model)) == "exact"
        }
        candidates.sort(key=lambda p: (p.id in exact, pick_order.key(p.id), -p.id), reverse=True)
    return candidates


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
    #: The chosen printer's queue header incarnation as this eligibility read it (A25): the placement refuses a
    #: header that is not this one.
    queue_identity: str | None = None

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
    deferred_printers: set[int] | None = None,
    pick_order: PickOrder | None = None,
) -> EligiblePrinter:
    """The best printer for ``item`` now, or why there is none.

    ``offline_feeds`` (an ``offline_feed.OfflineFeedCache``) lets a switched-off
    printer be described by what it lacks instead of as "offline": it is the
    same answer the wake step acts on, so the reason says why nothing was
    switched on (upstream #2876). Without it an off printer reads as offline.

    ``pick_order`` is the operator's printer order (C01): the AutoQueue pass reads it once and
    hands it in; without one it is read here when more than one candidate is left. It ranks in
    ``placement_rank`` only — never a filter.
    """
    if item.target_model:
        stopped: list[Printer] = []
        printers, normalized_model, location_suffix = await printers_for_item(db, item, halted=stopped)
        if not printers and not stopped:
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
    # Memory only (G03 B, A23): persisted by the row's own placement or by the pass's end batch, never left a
    # pending change that another writer's flush would carry.
    set_committed_value(item, "plate_id", req.resolved_plate_id)
    if not item.target_model:
        set_committed_value(item, "target_model", req.model)
    # The printers whose queue is halted are no candidates (G03 E-3), but the job's waiting
    # reason names each of them: the operator's Resume is what would let them take it.
    halted: list[Printer] = []
    headers: dict[int, str | None] = {}
    printers, normalized_model, location_suffix = await printers_for_item(db, item, halted=halted, headers=headers)
    halted_ids = {printer.id for printer in halted}
    if not printers and not halted:
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
    # Printers whose queue a drying cycle holds («Wait for drying to complete»):
    # the job would sit there until the cycle ends, so an equally fit printer
    # that is not held wins. ⚠️ A RANK term, never a gate - a held printer
    # still takes the job when it is the only fit one, and the wait then shows
    # on its row (see the module docstring: routing is not dispatching).
    drying_held = await scheduler.drying_held_printer_ids(db)

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

    for printer in [*printers, *halted]:
        verdict = model_compatibility(req.model, printer_manager.effective_model_for(printer.id, printer.model))
        if verdict not in ("exact", "compatible"):
            refuse(printer, "model_mismatch", routing_detail("model_mismatch")["message"], {"file_model": req.model})
            continue
        if printer.id in halted_ids:
            # Before "busy": a halted queue with rows left in it is first of all halted.
            refuse(printer, "queue_halted", routing_detail("queue_halted")["message"])
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
        ready = (
            scheduler._is_printer_ready_for_dispatch(printer.id, require_plate_clear)
            and printer.id not in (interlocked_printers or ())
            and printer.id not in drying_held
        )
        candidates.append((ready, verdict == "exact", result.plan.color_matches, printer, result.plan, snapshot))
    if candidates:
        from backend.app.services.auto_queue_return_policy import last_failed_printers, returned_printer_rank

        if deferred_printers is None:
            deferred_printers = (
                await deferred_printer_ids(db, datetime.now(timezone.utc)) if len(candidates) > 1 else set()
            )
        failed_identities = last_failed_printers(item)
        if pick_order is None and len(candidates) > 1:
            pick_order = await read_pick_order(db, now=datetime.now(timezone.utc))

        def rank(candidate):
            ready, exact, colours, printer, plan, _ = candidate
            ordinary = placement_rank(
                ready,
                exact,
                colours,
                printer.id in deferred_printers,
                printer.id,
                pick=pick_order.key(printer.id) if pick_order is not None else (),
                remaining_rank=plan.remaining_rank if prefer_lowest else 0,
            )
            return returned_printer_rank(failed_identities, printer.connector_identity, ordinary)

        _, _, _, printer, plan, snapshot = max(candidates, key=rank)
        return EligiblePrinter(
            printer,
            plan=plan,
            requirements=req,
            snapshot_signature=feed_signature(policy, snapshot),
            queue_identity=headers.get(printer.id),
        )
    facts.sort(key=lambda item: (item.printer_id or 0, item.code, repr(item.params)))
    if not facts:
        facts.append(fact("routing_unclassified", message=routing_detail("routing_unclassified")["message"]))
    return EligiblePrinter(reason=" | ".join(item.message for item in facts), requirements=req, facts=tuple(facts))
