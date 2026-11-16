"""A spool sits in ONE slot (owner decision Р7; upstream 6a6158c94).

The tables constrain the SLOT only — one row per (printer, AMS, tray). Nothing
stopped a second row naming the same spool: a tagged reel moved from T0 to T2
kept its T0 row whenever the partial-return rule held it, a hand "move" left
both rows, and a reel pre-assigned on one printer and loaded into another sat
on both. A print from the vacated slot was then charged to a spool that is not
in it, the slot was configured from it and K was bound for it.

So every writer that puts a spool into a slot takes it out of its other slots
through this module — one rule, both inventories. What it deliberately does
NOT do:

* it never touches the usage journal or a print session. A vacated slot is not
  a loaded one: no ``spool_loaded``, no tail, no runout is written. A running
  print is charged by the spool ids its start froze, whatever rows exist now;
* it never vacates the slot a running print feeds from. A hand move is REFUSED
  (:class:`SpoolFeedsPrint` → 409); an automatic writer leaves that row alone;
* it sends nothing to the printer of a vacated slot;
* it is not a database constraint. A UNIQUE on the spool column needs the
  existing duplicates resolved first — :func:`resolve_duplicates` does that as
  printers report in (owner decision Р36).

Built-in rows (``spool_assignment``) are deleted by ``on_ams_change`` through
the ORM under the printer's assignment lock, so a row of ANOTHER printer is
vacated only under that printer's lock: the hand route takes every lock it
needs in ascending printer id, the callback writers hand the other printers'
rows to :func:`release_elsewhere`, which runs after the callback has released
its own lock and holds one lock at a time. Spoolman rows are never deleted by
an AMS push, and every write here is a conditional statement, so they need no
foreign lock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import delete, select

from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

logger = logging.getLogger(__name__)

Slot = tuple[int, int, int]  # (printer_id, ams_id, tray_id)


class SpoolFeedsPrint(Exception):
    """The spool's other slot is feeding a running print: it stays where it is."""

    def __init__(self, slot: Slot):
        super().__init__(f"spool sits in a slot a running print feeds from: {slot}")
        self.slot = slot


@dataclass
class Released:
    # Slots whose row this call removed (not yet announced — see ``announce``).
    vacated: list[Slot] = field(default_factory=list)
    # Rows on printers whose lock the caller does not hold: for ``release_elsewhere``.
    deferred: list[Slot] = field(default_factory=list)
    # Rows left alone because their slot feeds a running print.
    feeding: list[Slot] = field(default_factory=list)


def global_tray(ams_id: int, tray_id: int) -> int:
    """The printer-wide tray number of a slot: AMS-HT is its unit id, the external holder 254/255."""
    if ams_id == 255:
        return 254 + tray_id
    if ams_id >= 128:
        return ams_id
    return ams_id * 4 + tray_id


def slot_feeds_running_print(printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Whether a print still to be charged on this printer feeds from this slot.

    The print's own facts decide: the trays its dispatched mapping names, plus
    the trays its tray-change log saw and the one loaded now (a backup switch
    feeds from a tray the mapping never named). A print whose mapping is not
    known — a restart restored no session, an external print that echoed none,
    Spoolman mode, which registers no session — may be feeding from ANY slot of
    that printer: unknown is "yes", never "no".
    """
    from backend.app.services import usage_tracker
    from backend.app.services.printer_manager import printer_manager

    state = printer_manager.get_status(printer_id)
    if not usage_tracker.print_is_unbooked(printer_id, state):
        return False
    session = usage_tracker._active_sessions.get(printer_id)
    mapping = getattr(session, "ams_mapping", None)
    used: set[int] = (
        {t for t in mapping if isinstance(t, int) and not isinstance(t, bool) and t >= 0}
        if isinstance(mapping, list)
        else set()
    )
    if not used:
        return True
    for entry in getattr(state, "tray_change_log", None) or []:
        try:
            used.add(int(entry[0]))
        except (TypeError, ValueError, IndexError):
            continue
    for attr in ("tray_now", "last_loaded_tray"):
        value = getattr(state, attr, None)
        # 255 is "nothing loaded" (and Ext-R only as a tray id the log would carry).
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 254:
            used.add(value)
    return global_tray(ams_id, tray_id) in used


async def printers_holding(db, *, spool_id: int, keep: Slot) -> set[int]:
    """The printers on which a built-in spool has a row outside ``keep`` — a read, taken before any lock."""
    rows = (
        await db.execute(
            select(SpoolAssignment.printer_id, SpoolAssignment.ams_id, SpoolAssignment.tray_id).where(
                SpoolAssignment.spool_id == spool_id
            )
        )
    ).all()
    return {printer_id for printer_id, ams_id, tray_id in rows if (printer_id, ams_id, tray_id) != keep}


async def other_slots(db, *, spool_id: int, keep: Slot) -> list[Slot]:
    """Every slot a built-in spool sits in besides ``keep`` — a read."""
    return [slot for _row_id, slot in await _other_rows(db, SpoolAssignment, SpoolAssignment.spool_id, spool_id, keep)]


async def other_spoolman_slots(db, *, spoolman_spool_id: int, keep: Slot) -> list[Slot]:
    """The same for a Spoolman spool's ledger rows."""
    rows = await _other_rows(
        db, SpoolmanSlotAssignment, SpoolmanSlotAssignment.spoolman_spool_id, spoolman_spool_id, keep
    )
    return [slot for _row_id, slot in rows]


async def _other_rows(db, model, column, spool_id: int, keep: Slot) -> list[tuple[int, Slot]]:
    rows = (
        await db.execute(
            select(model.id, model.printer_id, model.ams_id, model.tray_id)
            .where(column == spool_id)
            .order_by(model.printer_id, model.ams_id, model.tray_id)
        )
    ).all()
    return [(row_id, (p, a, t)) for row_id, p, a, t in rows if (p, a, t) != keep]


async def _release(
    db, model, column, *, spool_id: int, keep: Slot, locked: set[int] | None, refuse: bool, skip=None
) -> Released:
    others = await _other_rows(db, model, column, spool_id, keep)
    out = Released()
    for _row_id, slot in others:
        if slot_feeds_running_print(*slot):
            if refuse:
                raise SpoolFeedsPrint(slot)
            out.feeding.append(slot)
    for row_id, slot in others:
        if slot in out.feeding or (skip is not None and skip(slot)):
            continue
        if locked is not None and slot[0] not in locked:
            out.deferred.append(slot)
            continue
        # Conditional on what was read: a row that changed hands meanwhile is not ours to remove.
        result = await db.execute(delete(model).where(model.id == row_id, column == spool_id))
        if result.rowcount:
            out.vacated.append(slot)
    if out.vacated:
        logger.info("Spool %s moved to %s: taken out of %s", spool_id, keep, out.vacated)
    if out.feeding:
        logger.info("Spool %s moved to %s: left in %s, which feeds a running print", spool_id, keep, out.feeding)
    return out


async def release_other_slots(db, *, spool_id: int, keep: Slot, locked: set[int], refuse: bool, skip=None) -> Released:
    """Take a built-in spool out of every slot but ``keep``. Does not commit.

    ``locked`` — the printers whose assignment lock the caller holds; rows of
    other printers come back as ``deferred``. ``refuse`` — a hand move: raise
    :class:`SpoolFeedsPrint` before anything is removed. ``skip(slot)`` — an
    automatic writer's reason to leave a row (its slot reports the same tag).
    """
    return await _release(
        db,
        SpoolAssignment,
        SpoolAssignment.spool_id,
        spool_id=spool_id,
        keep=keep,
        locked=locked,
        refuse=refuse,
        skip=skip,
    )


async def release_other_spoolman_slots(db, *, spoolman_spool_id: int, keep: Slot, refuse: bool) -> Released:
    """The same for a Spoolman spool's ledger rows, on every printer. Does not commit."""
    return await _release(
        db,
        SpoolmanSlotAssignment,
        SpoolmanSlotAssignment.spoolman_spool_id,
        spool_id=spoolman_spool_id,
        keep=keep,
        locked=None,
        refuse=refuse,
    )


async def announce(vacated: list[Slot]) -> None:
    """Tell open tabs and the advertised-profile overlay about vacated slots — AFTER the commit."""
    if not vacated:
        return
    from backend.app.core.websocket import ws_manager
    from backend.app.services import ams_advertised_overlay as overlay

    for printer_id, ams_id, tray_id in vacated:
        overlay.forget(printer_id, ams_id, tray_id)
        try:
            await ws_manager.broadcast(
                {"type": "spool_assignment_changed", "printer_id": printer_id, "ams_id": ams_id, "tray_id": tray_id}
            )
        except Exception:  # noqa: BLE001 — a missed refresh, never the assignment's outcome
            logger.debug("spool_assignment_changed broadcast failed for %s", (printer_id, ams_id, tray_id))


def refusal(error: SpoolFeedsPrint):
    """The 409 a hand move answers with."""
    from fastapi import HTTPException

    return HTTPException(
        status_code=409,
        detail={
            "error": "spool_feeds_print",
            "message": "This spool is in a slot a running print feeds from; it can be moved when the print ends",
        },
    )


# ── rows of other printers, for writers that run inside a printer's AMS callback ──

_pending_elsewhere: dict[int, list[tuple[int, Slot]]] = {}
_pending_announce: dict[int, list[Slot]] = {}


def defer_elsewhere(printer_id: int, spool_id: int, keep: Slot) -> None:
    """Remember that ``spool_id`` now sits in ``keep`` and may still have rows on other printers."""
    _pending_elsewhere.setdefault(printer_id, []).append((spool_id, keep))


async def release_for_callback(db, *, printer_id: int, spool, keep: Slot) -> Released:
    """An automatic writer inside ``printer_id``'s AMS callback put ``spool`` into ``keep``.

    Its other rows on this printer go now, in the caller's transaction (the
    caller commits); rows on other printers and the announcements wait for
    :func:`release_elsewhere`. A row whose slot reports the spool's own tag, or
    feeds a running print, stays.
    """
    released = await release_other_slots(
        db, spool_id=spool.id, keep=keep, locked={printer_id}, refuse=False, skip=reports_tag_of(spool)
    )
    if released.vacated:
        _pending_announce.setdefault(printer_id, []).extend(released.vacated)
    if released.deferred:
        defer_elsewhere(printer_id, spool.id, keep)
    return released


async def release_elsewhere(printer_id: int) -> None:
    """Vacate the other printers' rows the callback of ``printer_id`` left for later.

    Called with NO assignment lock held; takes one printer's lock at a time, so
    two printers vacating each other's rows can never wait on each other.
    """
    await announce(_pending_announce.pop(printer_id, None) or [])
    pending = _pending_elsewhere.pop(printer_id, None)
    if not pending:
        return
    from backend.app.core.database import async_session
    from backend.app.main import _get_ams_assignment_lock

    for spool_id, keep in pending:
        try:
            async with async_session() as db:
                printers = sorted(await printers_holding(db, spool_id=spool_id, keep=keep))
            for other in printers:
                if other == printer_id:
                    continue  # the callback vacated its own printer's rows itself
                async with _get_ams_assignment_lock(other), async_session() as db:
                    # The move is still true only while ``keep`` still names the spool.
                    still_there = await db.scalar(
                        select(SpoolAssignment.id).where(
                            SpoolAssignment.printer_id == keep[0],
                            SpoolAssignment.ams_id == keep[1],
                            SpoolAssignment.tray_id == keep[2],
                            SpoolAssignment.spool_id == spool_id,
                        )
                    )
                    if still_there is None:
                        break
                    released = await release_other_slots(
                        db, spool_id=spool_id, keep=keep, locked={other}, refuse=False, skip=_reports_tag_of(spool_id)
                    )
                    await db.commit()
                await announce(released.vacated)
        except Exception:  # noqa: BLE001 — a duplicate row survives until the next pass, nothing else
            logger.exception("Could not take spool %s out of its slots on other printers", spool_id)


# ── which row a tag proves ──

_tags: dict[int, tuple[str, str]] = {}


def _live_tray(slot: Slot) -> dict | None:
    from backend.app.services.printer_manager import printer_manager

    printer_id, ams_id, tray_id = slot
    state = printer_manager.get_status(printer_id)
    raw = getattr(state, "raw_data", None) or {}
    if ams_id == 255:
        for vt in raw.get("vt_tray") or []:
            if isinstance(vt, dict) and str(vt.get("id")) == str(254 + tray_id):
                return vt
        return None
    ams = raw.get("ams") or []
    if isinstance(ams, dict):
        ams = ams.get("ams") or []
    for unit in ams:
        if not isinstance(unit, dict) or str(unit.get("id")) != str(ams_id):
            continue
        for tray in unit.get("tray") or []:
            if isinstance(tray, dict) and str(tray.get("id")) == str(tray_id):
                return tray
    return None


def slot_reports_tag(slot: Slot, tray_uuid: str | None, tag_uid: str | None) -> bool:
    """Whether the slot's live tray reports this reel's own tag right now."""
    from backend.app.services.spool_tag_matcher import is_valid_tag

    tray = _live_tray(slot)
    if not tray:
        return False
    live_uuid = str(tray.get("tray_uuid") or "").upper()
    live_uid = str(tray.get("tag_uid") or "").upper()
    if not is_valid_tag(live_uid, live_uuid):
        return False
    wanted_uuid = (tray_uuid or "").upper()
    wanted_uid = (tag_uid or "").upper()
    if wanted_uuid and set(wanted_uuid) != {"0"} and live_uuid == wanted_uuid:
        return True
    return bool(wanted_uid and set(wanted_uid) != {"0"} and live_uid == wanted_uid)


def remember_tag(spool) -> None:
    """Keep a tagged spool's tag for :func:`release_elsewhere`, which has no Spool row in hand."""
    _tags[spool.id] = (getattr(spool, "tray_uuid", None) or "", getattr(spool, "tag_uid", None) or "")


def _reports_tag_of(spool_id: int):
    tray_uuid, tag_uid = _tags.get(spool_id, ("", ""))

    def skip(slot: Slot) -> bool:
        return slot_reports_tag(slot, tray_uuid, tag_uid)

    return skip


def reports_tag_of(spool):
    """``skip`` for an automatic writer: leave a row whose slot reports the spool's own tag."""
    remember_tag(spool)
    return _reports_tag_of(spool.id)


# ── rows that were already double before the rule existed (owner decision Р36) ──


async def resolve_duplicates(db, printer_id: int) -> Released:
    """Settle spools of ``printer_id`` that also sit in another slot. Does not commit.

    Keep the row whose slot reports the spool's tag right now, else the newest;
    the others go — on this printer here, on other printers through
    :func:`release_elsewhere`. A row feeding a running print is never touched,
    and a tagged spool whose other printer has not reported in yet waits: its
    tag may be exactly there.
    """
    from sqlalchemy.orm import selectinload

    from backend.app.services.printer_manager import printer_manager

    mine = (
        (
            await db.execute(
                select(SpoolAssignment)
                .options(selectinload(SpoolAssignment.spool))
                .where(SpoolAssignment.printer_id == printer_id)
            )
        )
        .scalars()
        .all()
    )
    out = Released()
    for spool_id in sorted({row.spool_id for row in mine}):
        rows = (
            await db.execute(
                select(
                    SpoolAssignment.id,
                    SpoolAssignment.printer_id,
                    SpoolAssignment.ams_id,
                    SpoolAssignment.tray_id,
                    SpoolAssignment.created_at,
                ).where(SpoolAssignment.spool_id == spool_id)
            )
        ).all()
        if len(rows) < 2:
            continue
        spool = next(row.spool for row in mine if row.spool_id == spool_id)
        tray_uuid = getattr(spool, "tray_uuid", None) or ""
        tag_uid = getattr(spool, "tag_uid", None) or ""
        tagged = bool((tray_uuid and set(tray_uuid) != {"0"}) or (tag_uid and set(tag_uid) != {"0"}))
        slots = {row_id: (p, a, t) for row_id, p, a, t, _created in rows}
        if tagged and any(
            not getattr(printer_manager.get_status(slot[0]), "connected", False) for slot in slots.values()
        ):
            continue
        proven = [row_id for row_id, slot in slots.items() if tagged and slot_reports_tag(slot, tray_uuid, tag_uid)]
        if proven:
            keeper = proven[0]
        else:
            keeper = max(rows, key=lambda row: (row[4] is not None, row[4], row[0]))[0]
        keep = slots[keeper]
        logger.info("Spool %s sits in %d slots %s: keeping %s", spool_id, len(rows), sorted(slots.values()), keep)
        if tagged:
            remember_tag(spool)
        released = await release_other_slots(
            db, spool_id=spool_id, keep=keep, locked={printer_id}, refuse=False, skip=None
        )
        out.vacated += released.vacated
        out.feeding += released.feeding
        if released.deferred:
            out.deferred += released.deferred
            defer_elsewhere(printer_id, spool_id, keep)
    return out
