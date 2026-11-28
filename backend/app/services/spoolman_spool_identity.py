"""Which Spoolman spool a binding names: its number AND Spoolman's ``registered`` stamp.

Spec: vault ``60-specs/spoolman-binding-identity-spec.md``.

``spoolman_slot_assignments`` (AMS slot → spool) and ``spoolman_k_profile`` (spool →
K-profile) keep, beside the Spoolman number, the ``registered`` stamp of the spool the
writer proved (m266). Spoolman sets that stamp itself when it creates a spool, in whole
UTC seconds, and no client can set or change it; the number alone proves nothing —
Spoolman hands the number of a deleted spool to the next one, and another database
behind the address reuses every number.

- **A writer** stamps the row it writes and adopts its proof for the number's unstamped
  rows in both tables (:func:`adopt`), in its own transaction.
- **A confirming reader** — whatever writes something to Spoolman or to the printer from a
  binding's number: a charge, a weight, a drying stamp, a slot plan, a K — compares the
  stamp with the spool it holds (:func:`verdict`). ``unknown`` (a row from before m266)
  adopts that spool (owner decision 2026-11-27, trust on first read); ``other`` and
  ``gone`` write nothing and remove the binding (:func:`drop_stale`).
- **Not during a print's accounting** (spec r1 B04): a journal number is confirmed at the
  end of the print through the rows that name it then, so the rows of a printer whose
  print accounts in Spoolman are left to that print's tail (:func:`evidence_held`).
- **A change of the Spoolman database** removes every binding and K link
  (:func:`clear_all`, from ``spoolman_source_changes.SourceChange.decide``).

``gone`` is Spoolman's own "no such spool" only (``spoolman.says_spool_not_found``): a
proxy's 404 is Spoolman not answering, and removes nothing.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

SAME = "same"
UNKNOWN = "unknown"
OTHER = "other"
GONE = "gone"

# The stamp's column width (m266): Spoolman writes ``2024-05-01T12:00:00Z``.
STAMP_LENGTH = 40


def instant(value: str | None) -> datetime | None:
    """A Spoolman timestamp as naive UTC, or None when it is not one.

    The one reading of a ``registered`` stamp — usage history's ``belongs_to`` reads it
    through here too: ``...Z`` and ``...+00:00`` are the same moment.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def stamp_of(spool: dict | None) -> str | None:
    """The spool's ``registered`` as the binding stores it, or None when it has none."""
    if not isinstance(spool, dict):
        return None
    registered = spool.get("registered")
    if not registered or instant(str(registered)) is None:
        return None
    return str(registered)[:STAMP_LENGTH]


def verdict(stored: str | None, spool: dict | None) -> str:
    """What a binding's stamp says about the spool its number names now.

    ``spool`` None is Spoolman's own "no such spool" (:data:`GONE`). A spool without a
    readable stamp cannot refute anything (:data:`SAME`); a row without one is a row from
    before m266 (:data:`UNKNOWN`).
    """
    if spool is None:
        return GONE
    current = instant(stamp_of(spool))
    if current is None:
        return SAME
    own = instant(stored)
    if own is None:
        return UNKNOWN
    return SAME if own == current else OTHER


async def adopt(db: AsyncSession, number: int, stamp: str | None) -> None:
    """The number's unstamped rows in both tables take ``stamp`` — the caller's transaction.

    ``IS NULL`` keeps it idempotent and never overwrites a stamp another writer set.
    """
    from backend.app.models.spoolman_k_profile import SpoolmanKProfile  # noqa: PLC0415
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment  # noqa: PLC0415

    if not stamp:
        return
    for model in (SpoolmanSlotAssignment, SpoolmanKProfile):
        await db.execute(
            update(model)
            .where(model.spoolman_spool_id == number, model.spool_registered.is_(None))
            .values(spool_registered=stamp)
            .execution_options(synchronize_session=False)
        )


async def evidence_held(db: AsyncSession, printer_id: int) -> bool:
    """Whether the printer's slot rows are a print's evidence now (spec r1 B04).

    A journal number is confirmed at the end of its print through the slot rows that name
    it then; a row removed meanwhile would leave the number with nothing to refute it, and
    the tail would charge it to the spool the number names now. So while the printer has a
    print going (``printing``), its Spoolman tracking row, or any completion's accounting
    window is open, the rows stay — a reader only declines to write.
    """
    from backend.app.models.active_print_spoolman import ActivePrintSpoolman  # noqa: PLC0415
    from backend.app.models.archive import PrintArchive  # noqa: PLC0415
    from backend.app.services import spoolman_activity  # noqa: PLC0415

    if spoolman_activity.window_open():
        return True
    printing = exists().where(PrintArchive.printer_id == printer_id, PrintArchive.status == "printing")
    tracking = exists().where(ActivePrintSpoolman.printer_id == printer_id)
    return bool(await db.scalar(select(printing | tracking)))


Slot = tuple[int, int, int]


async def remove_refuted(
    db: AsyncSession,
    number: int,
    current: str | None,
    *,
    keep_printers: frozenset[int] = frozenset(),
    keep_slots: frozenset[Slot] = frozenset(),
) -> list[Slot]:
    """Remove what the number's spool refutes — the caller's transaction, locks and commit.

    ``current`` is the stamp of the spool the number names now; None is Spoolman's own
    "no such spool". Slot rows go when their known stamp is another spool's, or all of them
    when the spool is gone; a row without a stamp is not refuted by a stamp (it adopts at
    its next confirming read). K links go when their stamp is not the current spool's —
    unstamped ones too: they were made for this number before m266, and a binding of the
    number has just proved false. Rows of ``keep_printers`` (a print's evidence) and the
    ``keep_slots`` a writer is about to overwrite stay; K links are no evidence and go
    regardless. Every DELETE is conditional on what was read.

    Returns the ``(printer_id, ams_id, tray_id)`` of the slot rows removed.
    """
    from backend.app.models.spoolman_k_profile import SpoolmanKProfile  # noqa: PLC0415
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment  # noqa: PLC0415

    rows = (
        await db.execute(
            select(
                SpoolmanSlotAssignment.id,
                SpoolmanSlotAssignment.printer_id,
                SpoolmanSlotAssignment.ams_id,
                SpoolmanSlotAssignment.tray_id,
                SpoolmanSlotAssignment.spool_registered,
            ).where(SpoolmanSlotAssignment.spoolman_spool_id == number)
        )
    ).all()
    current_instant = instant(current)
    if current is not None and current_instant is None:
        return []  # a spool without a readable stamp refutes nothing
    removed: list[Slot] = []
    for row in rows:
        slot = (row.printer_id, row.ams_id, row.tray_id)
        if row.printer_id in keep_printers or slot in keep_slots:
            continue
        own = instant(row.spool_registered)
        if current is not None and (own is None or own == current_instant):
            continue
        stamp_matches = (
            SpoolmanSlotAssignment.spool_registered.is_(None)
            if row.spool_registered is None
            else SpoolmanSlotAssignment.spool_registered == row.spool_registered
        )
        result = await db.execute(
            delete(SpoolmanSlotAssignment)
            .where(
                SpoolmanSlotAssignment.id == row.id,
                SpoolmanSlotAssignment.spoolman_spool_id == number,
                stamp_matches,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount:
            removed.append(slot)

    links = (
        await db.execute(
            select(SpoolmanKProfile.id, SpoolmanKProfile.spool_registered).where(
                SpoolmanKProfile.spoolman_spool_id == number
            )
        )
    ).all()
    refuted_links = [link.id for link in links if current is None or instant(link.spool_registered) != current_instant]
    if refuted_links:
        await db.execute(
            delete(SpoolmanKProfile)
            .where(SpoolmanKProfile.id.in_(refuted_links), SpoolmanKProfile.spoolman_spool_id == number)
            .execution_options(synchronize_session=False)
        )
    return removed


async def refute(
    db: AsyncSession,
    number: int,
    current: str | None,
    *,
    keep_slots: frozenset[Slot] = frozenset(),
    tail_of: int | None = None,
    keep_printers: frozenset[int] = frozenset(),
) -> list[Slot]:
    """:func:`remove_refuted` with every printer whose rows are a print's evidence kept.

    ``tail_of`` is the printer whose print's tail is asking: its own rows are no longer
    evidence for anyone else (spec r1 B04). ``keep_printers`` are kept besides: printers the
    caller knows to be printing again (:func:`drop_stale`'s ``while_idle``). The caller's
    transaction and locks.
    """
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment  # noqa: PLC0415

    printers = set(
        (
            await db.execute(
                select(SpoolmanSlotAssignment.printer_id).where(SpoolmanSlotAssignment.spoolman_spool_id == number)
            )
        )
        .scalars()
        .all()
    )
    keep = frozenset([p for p in sorted(printers) if p != tail_of and await evidence_held(db, p)]) | (
        keep_printers & printers
    )
    if keep:
        logger.warning(
            "[SPOOLMAN] Spool %s: a binding of printer(s) %s is refuted and kept until its print's accounting ends",
            number,
            sorted(keep),
        )
    return await remove_refuted(db, number, current, keep_printers=keep, keep_slots=keep_slots)


async def writer_proved(db: AsyncSession, number: int, stamp: str | None, *, own_slot: Slot | None) -> list[Slot]:
    """A writer proved ``number`` IS the spool stamped ``stamp`` — the caller's transaction.

    Before its own row is written (spec r1 B03): when a row or link of the number carries
    another known stamp, the number has named another spool, so what that refutes goes
    (:func:`refute`; ``own_slot``, the row the writer is about to overwrite, stays for it);
    then the number's unstamped rows in both tables adopt the proof — otherwise its old K
    links would stay unstamped for good and resolve for the next spool given the number.
    Returns the slot rows removed (the caller forgets their overlay and announces them
    after its commit).
    """
    from backend.app.models.spoolman_k_profile import SpoolmanKProfile  # noqa: PLC0415
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment  # noqa: PLC0415

    proved = instant(stamp)
    if proved is None:
        return []
    known = [
        *(
            await db.execute(
                select(SpoolmanSlotAssignment.spool_registered).where(
                    SpoolmanSlotAssignment.spoolman_spool_id == number,
                    SpoolmanSlotAssignment.spool_registered.is_not(None),
                )
            )
        )
        .scalars()
        .all(),
        *(
            await db.execute(
                select(SpoolmanKProfile.spool_registered).where(
                    SpoolmanKProfile.spoolman_spool_id == number, SpoolmanKProfile.spool_registered.is_not(None)
                )
            )
        )
        .scalars()
        .all(),
    ]
    removed: list[Slot] = []
    if any(instant(own) not in (None, proved) for own in known):
        removed = await refute(db, number, stamp, keep_slots=frozenset([own_slot]) if own_slot else frozenset())
    await adopt(db, number, stamp)
    return removed


def reports_no_print(printer_id: int) -> bool:
    """The printer is connected and reports a state that is not a print — read in memory.

    A printer not connected, or one reporting nothing yet, is not known to be idle.
    """
    from backend.app.services.printer_manager import PrinterManager, printer_manager  # noqa: PLC0415

    state = printer_manager.get_status(printer_id)
    reported = (getattr(state, "state", "") or "").upper()
    return (
        bool(getattr(state, "connected", False))
        and bool(reported)
        and reported not in PrinterManager.ACTIVE_PRINT_STATES
    )


def forget_overlay(removed: list[tuple[int, int, int]]) -> None:
    """With the rows gone there is no spool behind those slots' masks (as the unassign route)."""
    from backend.app.services import ams_advertised_overlay as overlay  # noqa: PLC0415

    for printer_id, ams_id, tray_id in removed:
        overlay.forget(printer_id, ams_id, tray_id)


async def drop_stale(
    number: int,
    current: str | None,
    reason: str,
    *,
    tail_of: int | None = None,
    while_idle: int | None = None,
) -> list:
    """Remove a binding its spool refuted — under the slot-ledger doors, in its own session.

    ``current`` / ``reason`` as :func:`remove_refuted`; ``tail_of`` is the printer whose
    print's tail is calling: that print's own rows are no longer evidence for anyone else.
    ``while_idle`` is a printer whose rows go only while it reports no print — asked again
    under its lock: one printing again keeps them as its next print's evidence, for that
    print's own tail (the tail's sweep of the printer's other slots).
    Takes the slot-ledger write record, asks the mode again under it, then each printer's
    assignment lock in ascending order — the doors every ledger writer takes. Never called
    from inside a sync write, which already holds its printer's lock (spec r1 B09). Never
    raises. Returns the slots removed.
    """
    from backend.app.core.database import async_session  # noqa: PLC0415
    from backend.app.main import _get_ams_assignment_lock  # noqa: PLC0415
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment  # noqa: PLC0415
    from backend.app.services import spool_single_slot, spoolman_activity, spoolman_source_changes  # noqa: PLC0415

    removed: list[Slot] = []
    try:
        async with spoolman_activity.slot_write("spoolman_stale_binding"), async_session() as db:
            if not await spoolman_source_changes.mode_on(db):
                return []
            printers = sorted(
                set(
                    (
                        await db.execute(
                            select(SpoolmanSlotAssignment.printer_id).where(
                                SpoolmanSlotAssignment.spoolman_spool_id == number
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            )
            await db.commit()  # no read transaction is carried into the locks
            async with contextlib.AsyncExitStack() as stack:
                for printer_id in printers:
                    await stack.enter_async_context(_get_ams_assignment_lock(printer_id))
                printing_again = (
                    frozenset([while_idle])
                    if while_idle is not None and not reports_no_print(while_idle)
                    else frozenset()
                )
                removed = await refute(db, number, current, tail_of=tail_of, keep_printers=printing_again)
                await db.commit()
    except Exception:  # noqa: BLE001 — a removal that fails leaves the binding refuted, never a crash
        logger.exception("[SPOOLMAN] Could not remove the refuted binding of spool %s", number)
        return []
    forget_overlay(removed)
    for printer_id, ams_id, tray_id in removed:
        logger.warning(
            "[SPOOLMAN] Slot %s/%s/%s: binding to spool %s removed — %s (Spoolman's spool is %s)",
            printer_id,
            ams_id,
            tray_id,
            number,
            reason,
            "gone" if current is None else f"registered {current}",
        )
    await spool_single_slot.announce(removed)
    return removed


async def release_archived_slots(printer_id: int, spools: dict[int, dict]) -> list[Slot]:
    """Release ``printer_id``'s slots whose Spoolman spool is archived (upstream 23d04c864).

    A spool archived in Spoolman itself kept its BamDude binding: the next print from the
    slot was charged to it and the card kept showing it. A print's tail releases it once its
    own charges are written -- the tail owns that printer's rows (:func:`refute`'s
    ``tail_of``) -- and only while the printer reports no print running: a print that started
    meanwhile keeps them as its evidence, for its own tail. A row goes only for the spool it
    was proved for (:func:`verdict` same or unknown): one the slot got for another spool
    since stays. ``spools`` maps a number to its archived spool's record. Under the
    slot-ledger doors, in its own session. Never raises. Returns the slots released.
    """
    from backend.app.core.database import async_session  # noqa: PLC0415
    from backend.app.main import _get_ams_assignment_lock  # noqa: PLC0415
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment  # noqa: PLC0415
    from backend.app.services import spool_single_slot, spoolman_activity, spoolman_source_changes  # noqa: PLC0415

    if not spools or not reports_no_print(printer_id):
        return []
    released: list[Slot] = []
    try:
        async with spoolman_activity.slot_write("spoolman_archived_spool"), async_session() as db:
            if not await spoolman_source_changes.mode_on(db):
                return []
            await db.commit()  # no read transaction is carried into the lock
            async with _get_ams_assignment_lock(printer_id):
                if not reports_no_print(printer_id):
                    return []  # a print started while the lock was awaited: its own tail decides
                rows = (
                    (
                        await db.execute(
                            select(SpoolmanSlotAssignment).where(
                                SpoolmanSlotAssignment.printer_id == printer_id,
                                SpoolmanSlotAssignment.spoolman_spool_id.in_(sorted(spools)),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in rows:
                    if verdict(row.spool_registered, spools[row.spoolman_spool_id]) not in (SAME, UNKNOWN):
                        continue
                    released.append((row.printer_id, row.ams_id, row.tray_id))
                    await db.delete(row)
                await db.commit()
    except Exception:  # noqa: BLE001 — a release that fails leaves the binding for the next tail, never a crash
        logger.exception("[SPOOLMAN] Could not release the slots of archived spools on printer %s", printer_id)
        return []
    forget_overlay(released)
    for slot_printer, ams_id, tray_id in released:
        logger.info(
            "[SPOOLMAN] Slot %s/%s/%s: its spool is archived in Spoolman — the binding is released",
            slot_printer,
            ams_id,
            tray_id,
        )
    await spool_single_slot.announce(released)
    return released


async def adopt_now(number: int, stamp: str | None) -> None:
    """:func:`adopt` in its own session, under the slot-ledger doors. Never raises."""
    from backend.app.core.database import async_session  # noqa: PLC0415
    from backend.app.services import spoolman_activity, spoolman_source_changes  # noqa: PLC0415

    if not stamp:
        return
    try:
        async with spoolman_activity.slot_write("spoolman_adopt_stamp"), async_session() as db:
            if not await spoolman_source_changes.mode_on(db):
                return
            await adopt(db, number, stamp)
            await db.commit()
    except Exception:  # noqa: BLE001 — the next confirming read adopts again
        logger.exception("[SPOOLMAN] Could not stamp the bindings of spool %s", number)


async def binding_counts(db: AsyncSession) -> tuple[int, int]:
    """``(slot bindings, K links)`` a change of the Spoolman database would remove."""
    from backend.app.models.spoolman_k_profile import SpoolmanKProfile  # noqa: PLC0415
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment  # noqa: PLC0415

    slots = await db.scalar(select(func.count()).select_from(SpoolmanSlotAssignment)) or 0
    links = await db.scalar(select(func.count()).select_from(SpoolmanKProfile)) or 0
    return int(slots), int(links)


async def clear_all(db: AsyncSession) -> tuple[int, int]:
    """Remove every binding and K link: the Spoolman database changed (owner decision D2).

    The caller's transaction — ``SourceChange.decide`` — under its source guard; its door
    forgets the overlay after its last refusal. Returns what was removed.
    """
    from backend.app.models.spoolman_k_profile import SpoolmanKProfile  # noqa: PLC0415
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment  # noqa: PLC0415

    # Two literal statements, one per table: the content-delete census names each site.
    slots = await db.execute(delete(SpoolmanSlotAssignment))
    links = await db.execute(delete(SpoolmanKProfile))
    counts = (int(slots.rowcount or 0), int(links.rowcount or 0))
    logger.info("Cleared %d Spoolman slot bindings and %d K links: the Spoolman database changed", counts[0], counts[1])
    return counts


# The journal's number is confirmed through the slot rows that name it at the tail (§5.1).
FROM_ROWS = object()


class Admission:
    """Whether a print's tail may charge a number, and the spool it read to decide."""

    __slots__ = ("ok", "spool")

    def __init__(self, ok: bool, spool: dict | None) -> None:
        self.ok = ok
        self.spool = spool


class ChargeCheck:
    """One print's tail: may a number a binding or the journal gave be charged? (spec §5.1)

    A number from a slot binding (``slot_stamp``: the row's own stamp) or from the
    print's journal (:data:`FROM_ROWS`: the slot rows that name it now) is charged only
    when the spool it names in Spoolman is the bound one. The spool is read once per
    number for the whole print. ``unknown`` adopts it; ``other`` and ``gone`` are not
    charged — the grams go to :attr:`changed` for the notice — and the binding is removed
    by :meth:`settle` once the charges are written: this print's own rows are no longer
    evidence. A spool Spoolman could not be asked about is charged as before (the charge
    itself then fails or lands). A number from the tray's tag is its own proof and never
    comes here.
    """

    def __init__(self, client, printer_id: int, session_factory=None) -> None:
        """``session_factory`` — the caller's session maker (the tracking module's own); the
        application's by default."""
        self._client = client
        self._printer_id = printer_id
        self._session_factory = session_factory
        self._spools: dict[int, dict | None | Exception] = {}
        self._adopt: dict[int, str | None] = {}
        self._refuted: dict[int, str | None] = {}
        # (global tray id, grams, Spoolman number) not charged: the number names another spool.
        self.changed: list[tuple[int, float, int]] = []

    async def _spool(self, number: int) -> dict | None | Exception:
        if number not in self._spools:
            from backend.app.services.spoolman import SpoolmanNotFoundError  # noqa: PLC0415

            try:
                spool = await self._client.get_spool(number)
                # An answer that is no spool record tells nothing: unknown, as no answer.
                self._spools[number] = spool if isinstance(spool, dict) else ValueError("not a spool record")
            except SpoolmanNotFoundError:
                self._spools[number] = None
            except Exception as exc:  # noqa: BLE001 — unknown, so nothing is refuted
                self._spools[number] = exc
        return self._spools[number]

    async def _row_verdict(self, number: int, spool: dict | None) -> str | None:
        """The verdict of the slot rows naming ``number`` now; None when no row does."""
        from backend.app.core.database import async_session  # noqa: PLC0415
        from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment  # noqa: PLC0415

        async with (self._session_factory or async_session)() as db:
            stamps = (
                (
                    await db.execute(
                        select(SpoolmanSlotAssignment.spool_registered).where(
                            SpoolmanSlotAssignment.spoolman_spool_id == number
                        )
                    )
                )
                .scalars()
                .all()
            )
        if not stamps:
            return None
        verdicts = {verdict(stamp, spool) for stamp in stamps}
        for answer in (GONE, OTHER, UNKNOWN):
            if answer in verdicts:
                return answer
        return SAME

    async def judge(self, number: int, spool: dict | None, *, slot_stamp=FROM_ROWS) -> bool:
        """With the spool already read (``None``: Spoolman's own "no such spool")."""
        judged = verdict(slot_stamp, spool) if slot_stamp is not FROM_ROWS else await self._row_verdict(number, spool)
        if judged is None or judged == SAME:
            return True
        if judged == UNKNOWN:
            self._adopt[number] = stamp_of(spool)
            return True
        self._refuted[number] = stamp_of(spool)
        logger.warning(
            "[SPOOLMAN] Spool %s: the binding names %s in Spoolman now — not charged",
            number,
            "no spool" if spool is None else "another spool",
        )
        return False

    async def admits(self, number: int, *, slot_stamp=FROM_ROWS, tray: int, grams: float) -> Admission:
        """Read the number's spool (once per print) and judge it; refused grams are noted."""
        spool = await self._spool(number)
        if isinstance(spool, Exception):
            return Admission(True, None)
        if await self.judge(number, spool, slot_stamp=slot_stamp):
            return Admission(True, spool)
        if grams > 0:
            self.changed.append((tray, round(float(grams), 2), number))
        return Admission(False, spool)

    async def settle(self) -> None:
        """After the charges: adopt what was confirmed, remove what was refuted, then let the
        printer's slots go whose spool Spoolman has archived or no longer has. Never raises."""
        for number, stamp in self._adopt.items():
            if number not in self._refuted:
                await adopt_now(number, stamp)
        for number, stamp in self._refuted.items():
            await drop_stale(
                number,
                stamp,
                "the spool is gone from Spoolman" if stamp is None else "the number names another spool in Spoolman",
                tail_of=self._printer_id,
            )
        await self._release_archived_or_gone()

    async def _release_archived_or_gone(self) -> None:
        """Every slot of the printer, also one this print did not feed from (upstream 23d04c864).

        A spool archived in Spoolman itself is released (:func:`release_archived_slots`); one
        Spoolman no longer has (its own "no such spool") is refuted as for a charge. Each
        number is read once for the whole print, as the charges read theirs. Both only while
        the printer reports no print, asked again under its lock: a print that started since
        keeps every row as its evidence, for its own tail -- a gone number Spoolman gives to
        a new spool must not lose the row that refutes it (acceptance A1-01).
        """
        from backend.app.core.database import async_session  # noqa: PLC0415
        from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment  # noqa: PLC0415

        if not reports_no_print(self._printer_id):
            return
        try:
            async with (self._session_factory or async_session)() as db:
                numbers = sorted(
                    set(
                        (
                            await db.execute(
                                select(SpoolmanSlotAssignment.spoolman_spool_id).where(
                                    SpoolmanSlotAssignment.printer_id == self._printer_id
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                )
        except Exception:  # noqa: BLE001 — the next print's tail asks again
            logger.exception("[SPOOLMAN] Could not read the slots of printer %s", self._printer_id)
            return
        archived: dict[int, dict] = {}
        for number in numbers:
            if number in self._refuted:
                continue  # removed above
            spool = await self._spool(number)
            if spool is None:
                await drop_stale(
                    number,
                    None,
                    "the spool is gone from Spoolman",
                    tail_of=self._printer_id,
                    while_idle=self._printer_id,
                )
            elif isinstance(spool, dict) and spool.get("archived"):
                archived[number] = spool
        await release_archived_slots(self._printer_id, archived)
