"""What a Spoolman AMS sync may write to ``spoolman_slot_assignments``.

The two Sync buttons read the ledger, then ask Spoolman once per tray, then
write. An operator who assigns or unassigns a slot by hand while those requests
are in flight used to lose: the sync upserted every slot it had resolved and
deleted every slot it had seen empty, whatever the row said by then, and —
comparing with the ledger it had read — announced nothing (upstream fcc20cb62).

So a sync writes only the slots whose spool it CHANGED, and only where the slot
still holds what it read; the write runs under the per-printer assignment lock
the assign routes take, and what is announced is what the database says moved
(``rowcount``), never the snapshot.
"""

from __future__ import annotations

from sqlalchemy import text

_SLOT = "printer_id = :printer_id AND ams_id = :ams_id AND tray_id = :tray_id"

_UPSERT = text(
    "INSERT INTO spoolman_slot_assignments (printer_id, ams_id, tray_id, spoolman_spool_id)"
    " VALUES (:printer_id, :ams_id, :tray_id, :spool_id)"
    " ON CONFLICT(printer_id, ams_id, tray_id)"
    " DO UPDATE SET spoolman_spool_id = excluded.spoolman_spool_id"
)
_INSERT_IF_STILL_EMPTY = text(
    "INSERT INTO spoolman_slot_assignments (printer_id, ams_id, tray_id, spoolman_spool_id)"
    " VALUES (:printer_id, :ams_id, :tray_id, :spool_id)"
    " ON CONFLICT(printer_id, ams_id, tray_id) DO NOTHING"
)
_REPOINT_IF_UNCHANGED = text(
    f"UPDATE spoolman_slot_assignments SET spoolman_spool_id = :spool_id WHERE {_SLOT} AND spoolman_spool_id = :read"
)
_DELETE_IF_UNCHANGED = text(f"DELETE FROM spoolman_slot_assignments WHERE {_SLOT} AND spoolman_spool_id = :read")
_DELETE = text(f"DELETE FROM spoolman_slot_assignments WHERE {_SLOT}")


async def write_sync_results(
    db,
    *,
    printer_id: int,
    read: dict[tuple[int, int], int] | None,
    changes: list[tuple[int, int, int]],
    empties: list[tuple[int, int]],
    issued: int | None = None,
) -> list[tuple[int, int]]:
    """Apply one printer's sync results; return the ``(ams_id, tray_id)`` that moved.

    ``read`` is the printer's ledger as the sync loaded it, ``(ams_id, tray_id)``
    → spool id. ``None`` means it could not be read: nothing can be compared, so
    the write is the unconditional one it always was and every slot is reported.

    ``changes`` are ``(ams_id, tray_id, spool_id)`` the sync resolved; ``empties``
    the slots it saw without a spool. Commits; raises after a rollback.

    ``issued`` is the binding ticket the sync took before loading its cache
    (C047 §C3): under the printer's lane, every number of ``changes`` enters the
    fence's guard, ascending, before the first DML, and stays in it through the
    commit or the rollback — ``SpoolBeingDeleted`` refuses the whole write.
    """
    from backend.app.main import _get_ams_assignment_lock  # noqa: PLC0415
    from backend.app.services import spool_single_slot, spoolman_binding_fence  # noqa: PLC0415

    moved: list[tuple[int, int]] = []
    vacated: list[tuple[int, int, int]] = []
    numbers = [spool_id for _ams, _tray, spool_id in changes]
    async with _get_ams_assignment_lock(printer_id):
        with spoolman_binding_fence.enter(numbers, spoolman_binding_fence.ticket() if issued is None else issued):
            await _apply(db, printer_id, read, changes, empties, moved, vacated)
    # This printer's own vacated slots are reported with ``moved``; the others
    # are announced here, after the commit.
    moved += [(a, t) for p, a, t in vacated if p == printer_id]
    await spool_single_slot.announce([slot for slot in vacated if slot[0] != printer_id])
    return list(dict.fromkeys(moved))


async def _apply(db, printer_id, read, changes, empties, moved, vacated) -> None:
    from backend.app.services import spool_single_slot  # noqa: PLC0415

    try:
        for ams_id, tray_id, spool_id in changes:
            slot = {"printer_id": printer_id, "ams_id": ams_id, "tray_id": tray_id}
            if read is None:
                await db.execute(_UPSERT, {**slot, "spool_id": spool_id})
                moved.append((ams_id, tray_id))
                continue
            was = read.get((ams_id, tray_id))
            if was == spool_id:
                # Nothing to change — and a hand unassignment made meanwhile stays.
                continue
            if was is None:
                result = await db.execute(_INSERT_IF_STILL_EMPTY, {**slot, "spool_id": spool_id})
            else:
                result = await db.execute(_REPOINT_IF_UNCHANGED, {**slot, "spool_id": spool_id, "read": was})
            if result.rowcount:
                moved.append((ams_id, tray_id))
        # A spool sits in one slot (owner decision Р7): a spool this sync
        # just put into a slot leaves its other ledger rows, on any printer —
        # except a slot that feeds a running print. Rows only; conditional on
        # what was read, as every write here.
        for ams_id, tray_id, spool_id in changes:
            if (ams_id, tray_id) not in moved:
                continue
            released = await spool_single_slot.release_other_spoolman_slots(
                db, spoolman_spool_id=spool_id, keep=(printer_id, ams_id, tray_id), refuse=False
            )
            vacated += released.vacated
        for ams_id, tray_id in empties:
            slot = {"printer_id": printer_id, "ams_id": ams_id, "tray_id": tray_id}
            if read is None:
                await db.execute(_DELETE, slot)
                moved.append((ams_id, tray_id))
                continue
            was = read.get((ams_id, tray_id))
            if was is None:
                # Read without a row: one that exists now is somebody's hand assignment.
                continue
            result = await db.execute(_DELETE_IF_UNCHANGED, {**slot, "read": was})
            if result.rowcount:
                moved.append((ams_id, tray_id))
        await db.commit()
    except Exception:
        await db.rollback()
        raise
