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

A row names its spool by number AND ``registered`` stamp (m266,
``services/spoolman_spool_identity``). What a sync resolved says where it came from
(spec ``spoolman-binding-identity`` r1 B01): a spool found by the tray's TAG is in the
tray, so the row takes its stamp — the same number with another stamp too; a spool
found by the ``hint`` — the binding's own number, for a tag-less tray — proves nothing,
so the row is JUDGED by it: another spool's stamp removes the row (outside a print's
accounting), an unstamped row adopts it, and a hint never stamps. That judgement runs
here, inside the write's own transaction and locks — never through ``drop_stale``,
which would take this printer's lock again (r1 B09).
"""

from __future__ import annotations

from typing import NamedTuple

from sqlalchemy import text

_SLOT = "printer_id = :printer_id AND ams_id = :ams_id AND tray_id = :tray_id"

_UPSERT = text(
    "INSERT INTO spoolman_slot_assignments (printer_id, ams_id, tray_id, spoolman_spool_id, spool_registered)"
    " VALUES (:printer_id, :ams_id, :tray_id, :spool_id, :stamp)"
    " ON CONFLICT(printer_id, ams_id, tray_id)"
    " DO UPDATE SET spoolman_spool_id = excluded.spoolman_spool_id, spool_registered = excluded.spool_registered"
)
_INSERT_IF_STILL_EMPTY = text(
    "INSERT INTO spoolman_slot_assignments (printer_id, ams_id, tray_id, spoolman_spool_id, spool_registered)"
    " VALUES (:printer_id, :ams_id, :tray_id, :spool_id, :stamp)"
    " ON CONFLICT(printer_id, ams_id, tray_id) DO NOTHING"
)
_REPOINT_IF_UNCHANGED = text(
    "UPDATE spoolman_slot_assignments SET spoolman_spool_id = :spool_id, spool_registered = :stamp"
    f" WHERE {_SLOT} AND spoolman_spool_id = :read"
)
_STAMP_OF = text(f"SELECT spool_registered FROM spoolman_slot_assignments WHERE {_SLOT} AND spoolman_spool_id = :read")
_DELETE_IF_UNCHANGED = text(f"DELETE FROM spoolman_slot_assignments WHERE {_SLOT} AND spoolman_spool_id = :read")
_DELETE = text(f"DELETE FROM spoolman_slot_assignments WHERE {_SLOT}")


class SyncChange(NamedTuple):
    """What a sync resolved for one tray."""

    ams_id: int
    tray_id: int
    spool_id: int
    # The resolved spool's Spoolman ``registered`` (``spoolman_spool_identity.stamp_of``).
    stamp: str | None
    # Found by the tray's tag (or created for it) — a proof; False: by the binding's hint.
    by_tag: bool


async def write_sync_results(
    db,
    *,
    printer_id: int,
    read: dict[tuple[int, int], int] | None,
    changes: list[SyncChange],
    empties: list[tuple[int, int]],
    issued: int | None = None,
) -> list[tuple[int, int]]:
    """Apply one printer's sync results; return the ``(ams_id, tray_id)`` that moved.

    ``read`` is the printer's ledger as the sync loaded it, ``(ams_id, tray_id)``
    → spool id. ``None`` means it could not be read: nothing can be compared, so
    the write is the unconditional one it always was and every slot is reported.

    ``changes`` are the :class:`SyncChange` the sync resolved; ``empties`` the
    slots it saw without a spool. Commits; raises after a rollback.

    ``issued`` is the binding ticket the sync took before loading its cache
    (C047 §C3): under the printer's lane, every number of ``changes`` enters the
    fence's guard, ascending, before the first DML, and stays in it through the
    commit or the rollback — ``SpoolBeingDeleted`` refuses the whole write.
    """
    from backend.app.main import _get_ams_assignment_lock  # noqa: PLC0415
    from backend.app.services import spool_single_slot, spoolman_binding_fence, spoolman_spool_identity  # noqa: PLC0415

    moved: list[tuple[int, int]] = []
    vacated: list[tuple[int, int, int]] = []
    numbers = [change.spool_id for change in changes]
    async with _get_ams_assignment_lock(printer_id):
        with spoolman_binding_fence.enter(numbers, spoolman_binding_fence.ticket() if issued is None else issued):
            refuted = await _apply(db, printer_id, read, changes, empties, moved, vacated)
    spoolman_spool_identity.forget_overlay(refuted)
    vacated += refuted
    # This printer's own vacated slots are reported with ``moved``; the others
    # are announced here, after the commit.
    moved += [(a, t) for p, a, t in vacated if p == printer_id]
    await spool_single_slot.announce([slot for slot in vacated if slot[0] != printer_id])
    return list(dict.fromkeys(moved))


async def _apply(db, printer_id, read, changes, empties, moved, vacated) -> list[tuple[int, int, int]]:
    """The conditional writes, one transaction. Returns the slot rows a stamp refuted."""
    from backend.app.services import (
        spool_single_slot,  # noqa: PLC0415
        spoolman_spool_identity as identity,  # noqa: PLC0415
    )

    refuted: list[tuple[int, int, int]] = []
    try:
        for change in changes:
            ams_id, tray_id, spool_id, stamp = change.ams_id, change.tray_id, change.spool_id, change.stamp
            slot = {"printer_id": printer_id, "ams_id": ams_id, "tray_id": tray_id}
            if not change.by_tag:
                refuted += await _judge_by_hint(db, printer_id, slot, spool_id, stamp, read)
                continue
            # Found by the tag: the proof's refutations go, the number's unstamped rows adopt it.
            refuted += await identity.writer_proved(db, spool_id, stamp, own_slot=(printer_id, ams_id, tray_id))
            if read is None:
                await db.execute(_UPSERT, {**slot, "spool_id": spool_id, "stamp": stamp})
                moved.append((ams_id, tray_id))
                continue
            was = read.get((ams_id, tray_id))
            if was == spool_id:
                # The same number: the row moves to the tag's spool when it named another one.
                own = (await db.execute(_STAMP_OF, {**slot, "read": was})).scalar_one_or_none()
                if identity.verdict(own, {"registered": stamp}) == identity.OTHER:
                    result = await db.execute(
                        _REPOINT_IF_UNCHANGED, {**slot, "spool_id": spool_id, "stamp": stamp, "read": was}
                    )
                    if result.rowcount:
                        moved.append((ams_id, tray_id))
                # Otherwise nothing to change — and a hand unassignment made meanwhile stays.
                continue
            if was is None:
                result = await db.execute(_INSERT_IF_STILL_EMPTY, {**slot, "spool_id": spool_id, "stamp": stamp})
            else:
                result = await db.execute(
                    _REPOINT_IF_UNCHANGED, {**slot, "spool_id": spool_id, "stamp": stamp, "read": was}
                )
            if result.rowcount:
                moved.append((ams_id, tray_id))
        # A spool sits in one slot (owner decision Р7): a spool this sync
        # just put into a slot leaves its other ledger rows, on any printer —
        # except a slot that feeds a running print. Rows only; conditional on
        # what was read, as every write here.
        for change in changes:
            if not change.by_tag or (change.ams_id, change.tray_id) not in moved:
                continue
            released = await spool_single_slot.release_other_spoolman_slots(
                db, spoolman_spool_id=change.spool_id, keep=(printer_id, change.ams_id, change.tray_id), refuse=False
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
    return refuted


async def _judge_by_hint(db, printer_id, slot, spool_id, stamp, read) -> list[tuple[int, int, int]]:
    """A tag-less tray's binding, judged by the spool its own number names now (r1 B01).

    A hint proves nothing, so it never stamps: another spool's stamp refutes the binding
    (:func:`spoolman_spool_identity.refute`, which keeps a print's evidence), an
    unstamped row adopts the spool (trust on first read). The row must still be the
    one the sync read.
    """
    from backend.app.services import spoolman_spool_identity as identity  # noqa: PLC0415

    if read is not None and read.get((slot["ams_id"], slot["tray_id"])) != spool_id:
        return []
    own = (await db.execute(_STAMP_OF, {**slot, "read": spool_id})).scalar_one_or_none()
    judged = identity.verdict(own, {"registered": stamp})
    if judged == identity.UNKNOWN:
        await identity.adopt(db, spool_id, stamp)
        return []
    if judged == identity.OTHER:
        return await identity.refute(db, spool_id, stamp)
    return []
