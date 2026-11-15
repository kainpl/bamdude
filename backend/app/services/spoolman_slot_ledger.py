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
) -> list[tuple[int, int]]:
    """Apply one printer's sync results; return the ``(ams_id, tray_id)`` that moved.

    ``read`` is the printer's ledger as the sync loaded it, ``(ams_id, tray_id)``
    → spool id. ``None`` means it could not be read: nothing can be compared, so
    the write is the unconditional one it always was and every slot is reported.

    ``changes`` are ``(ams_id, tray_id, spool_id)`` the sync resolved; ``empties``
    the slots it saw without a spool. Commits; raises after a rollback.
    """
    from backend.app.main import _get_ams_assignment_lock  # noqa: PLC0415

    moved: list[tuple[int, int]] = []
    async with _get_ams_assignment_lock(printer_id):
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
    return list(dict.fromkeys(moved))
