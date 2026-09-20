"""Eligibility evaluation for auto-queue items.

Given an AutoQueueItem and a set of printers already busy in the
current scheduler tick, pick the printer whose queue the item should join:

1. Matches ``target_model`` (case-insensitive, normalised).
2. Matches ``target_location`` if specified.
3. Has ``auto_distribute_eligible=True`` on its PrinterQueue.
4. Is connected over MQTT.
5. Has all ``required_filament_types`` loaded across AMS + external
   trays (canonical-type matching, so PA-CF / PA12-CF / PAHT-CF are
   equivalent — same as upstream).
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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.auto_queue_ams import _normalize_color_for_compare
from backend.app.services.filament_intake import read_item_requirements, routing_detail
from backend.app.services.filament_policy import auto_policy
from backend.app.services.filament_requirements import PrintRequirementsCache
from backend.app.services.filament_routing import resolve_filament_routing
from backend.app.services.print_scheduler import _canonical_filament_type, scheduler
from backend.app.services.printer_location_service import load_tree, path_of, subtree_ids
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.printer_models import normalize_model_name

logger = logging.getLogger(__name__)


def _get_missing_filament_types(printer_id: int, required_types: list[str]) -> list[str]:
    """Return the subset of ``required_types`` not loaded on the printer.

    Empty list means all required types are present. Uses canonical-type
    matching for equivalence groups.
    """
    status = printer_manager.get_status(printer_id)
    if not status:
        # Cannot determine; treat as "all missing" (caller skips the printer)
        return list(required_types)

    loaded: set[str] = set()
    for ams_unit in status.raw_data.get("ams", []) or []:
        for tray in ams_unit.get("tray", []) or []:
            t = tray.get("tray_type")
            if t:
                loaded.add(_canonical_filament_type(t))
    for vt in status.raw_data.get("vt_tray") or []:
        t = vt.get("tray_type")
        if t:
            loaded.add(_canonical_filament_type(t))

    return [t for t in required_types if _canonical_filament_type(t) not in loaded]


def _get_missing_force_color_slots(printer_id: int, force_overrides: list[dict]) -> list[str]:
    """For force_color_match overrides, return descriptive strings of unmatched slots.

    Each override must have ``type`` and ``color``. Returns ``"TYPE (color)"``
    for entries that don't have an exact type+color match on the printer.

    When both the override and a candidate tray carry a ``tray_info_idx``, they
    must match on that too. Bambu reports every PLA variant as
    ``tray_type == "PLA"`` — Basic / Matte / Silk are told apart only by the idx
    (GFA00 / GFA01 / GFA06), so without this a job sliced for PLA Matte was an
    exact match for any white PLA on the farm (#2650). A blank idx on **either**
    side falls back to the historical type+colour comparison: custom and
    third-party spools report no idx at all, and 3MFs sliced before the field
    existed carry none, so tightening that case would strand those setups
    instead of routing them.
    """
    status = printer_manager.get_status(printer_id)
    if not status:
        return [f"{o.get('type', '?')} ({o.get('color_name') or o.get('color', '?')})" for o in force_overrides]

    # A list of triples, not a set of pairs: two trays can share type+colour and
    # differ only by variant, and both have to stay visible to the comparison.
    loaded: list[tuple[str, str, str]] = []
    for ams_unit in status.raw_data.get("ams", []) or []:
        for tray in ams_unit.get("tray", []) or []:
            t = tray.get("tray_type")
            if t:
                loaded.append(
                    (
                        _canonical_filament_type(t),
                        _normalize_color_for_compare(tray.get("tray_color", "")),
                        tray.get("tray_info_idx", "") or "",
                    )
                )
    for vt in status.raw_data.get("vt_tray") or []:
        t = vt.get("tray_type")
        if t:
            loaded.append(
                (
                    _canonical_filament_type(t),
                    _normalize_color_for_compare(vt.get("tray_color", "")),
                    vt.get("tray_info_idx", "") or "",
                )
            )

    missing: list[str] = []
    for o in force_overrides:
        o_type = _canonical_filament_type(o.get("type") or "")
        o_color = _normalize_color_for_compare(o.get("color") or "")
        o_idx = o.get("tray_info_idx") or ""
        satisfied = any(
            t_type == o_type and t_color == o_color and (not o_idx or not t_idx or o_idx == t_idx)
            for t_type, t_color, t_idx in loaded
        )
        if not satisfied:
            color_label = o.get("color_name") or o.get("color", "?")
            missing.append(f"{o_type} ({color_label})")
    return missing


def _count_override_color_matches(printer_id: int, overrides: list[dict]) -> int:
    """Count overrides that have an exact type+color match on the printer.

    Used to rank printers when overrides are preferences, not hard requirements.

    Deliberately blind to ``tray_info_idx``, unlike the force path above. A
    preference override is a *swap*: the operator chose a different filament for
    that slot, so the 3MF's variant now describes the spool being replaced. It
    is also only a ranking input — pinning it here would demote printers that
    hold exactly what was asked for.
    """
    status = printer_manager.get_status(printer_id)
    if not status:
        return 0

    loaded: set[tuple[str, str]] = set()
    for ams_unit in status.raw_data.get("ams", []) or []:
        for tray in ams_unit.get("tray", []) or []:
            t = tray.get("tray_type")
            if t:
                loaded.add((t.upper(), _normalize_color_for_compare(tray.get("tray_color", ""))))
    for vt in status.raw_data.get("vt_tray") or []:
        t = vt.get("tray_type")
        if t:
            loaded.add((t.upper(), _normalize_color_for_compare(vt.get("tray_color", ""))))

    matches = 0
    for o in overrides:
        o_type = (o.get("type") or "").upper()
        o_color = _normalize_color_for_compare(o.get("color") or "")
        if (o_type, o_color) in loaded:
            matches += 1
    return matches


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
    printing = await db.execute(select(PrinterQueue.printer_id).where(PrinterQueue.status == "printing"))
    holding = await db.execute(
        select(PrinterQueue.printer_id)
        .join(PrintQueueItem, PrintQueueItem.queue_id == PrinterQueue.id)
        .where(PrintQueueItem.status == "pending")
        .distinct()
    )
    return {pid for (pid,) in printing.all()} | {pid for (pid,) in holding.all()}


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
            location_suffix = f" in {path_of(tree, item.target_location_id)}"

    result = await db.execute(query)
    printers = [p for p in result.scalars().all() if normalize_model_name(p.model) == normalized_model]
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
) -> EligiblePrinter:
    if item.target_model:
        printers, normalized_model, location_suffix = await printers_for_item(db, item)
        if not printers:
            return EligiblePrinter(reason=f"No active {normalized_model} printers{location_suffix} eligible")
    req = await read_item_requirements(db, item, cache)
    if req.status != "ok":
        return EligiblePrinter(reason=routing_detail(req.reason)["message"], requirements=req)
    item.plate_id = req.resolved_plate_id
    if not item.target_model:
        item.target_model = req.model
    printers, normalized_model, location_suffix = await printers_for_item(db, item)
    if not printers:
        return EligiblePrinter(reason=f"No active {normalized_model} printers{location_suffix} eligible")
    policy = auto_policy(item)
    candidates, reasons = [], []
    for printer in printers:
        if printer.id in busy_printers:
            reasons.append(f"{printer.name}: " + routing_detail("printer_busy")["message"])
            continue
        if item.require_previous_success and not await scheduler.previous_print_succeeded(db, printer.id):
            reasons.append(f"{printer.name}: " + routing_detail("previous_print_failed")["message"])
            continue
        result = resolve_filament_routing(
            req, policy, printer_manager.get_feed_snapshot(printer.id), prefer_lowest=prefer_lowest
        )
        if result.plan is None:
            reasons.append(f"{printer.name}: " + routing_detail(result.reason)["message"])
            continue
        ready = scheduler._is_printer_idle(printer.id, require_plate_clear)
        candidates.append((ready, result.plan.color_matches, -printer.id, printer, result.plan))
    if candidates:
        _, _, _, printer, plan = max(candidates, key=lambda c: c[:3])
        return EligiblePrinter(printer, plan=plan, requirements=req)
    return EligiblePrinter(reason=" | ".join(reasons))
