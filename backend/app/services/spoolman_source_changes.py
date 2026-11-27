"""The writers of the Spoolman source: one ownership for every door (C047 part 2, spec §C1).

A *source change* writes one of the three rows a delete's S0 reads —
``spoolman_enabled``, ``spoolman_url``, ``spoolman_usage_source`` — so that
their effective value changes. Every door that does (the Spoolman settings
route, the general settings save, reset, "same database" / "new database",
Git's settings category) owns it through :class:`SourceChange`:

1. it takes the source guard in a clean session before its first SQL write,
   and before the credential anchor (S30);
2. under it, after its reads and before its first write, :meth:`decide`:
   - no effective change → nothing more (a save of the same values changes no
     generation and is never refused);
   - any effective change while an accounting window or a charge is open →
     :class:`~spoolman_source.SourceRefused` ``charge_window``;
   - a change of the **mode** — the mode-change flag is set, so no new
     slot-ledger writer starts, and the change waits for those in flight
     (``spoolman_activity.slot_write``; ``slot_writes`` when they do not finish
     in time); then it clears the ledger of the mode it leaves, in the door's
     transaction — every door that moves the mode, not one route (acceptance A08,
     A09). A door that takes the credential anchor before ``decide`` waits
     before that anchor (:meth:`SourceChange.wait_before_anchor`, A12);
   - a change of the **database** (a new non-empty address, ``rotate``) —
     the base-change flag is set in one synchronous step with the check of the
     open activity records, and only then a FRESH read of the durable
     predicate runs (S44); a protected print → ``print_accounting``; then every
     Spoolman slot binding and K link is removed in the door's transaction —
     their numbers are the database's being left (owner decision 2026-11-27,
     ``spoolman_spool_identity.clear_all``). It waits for no slot writer: one
     that proved its spool against the old database commits a row carrying that
     database's stamp, which the first confirming reader refutes;
   - the generation goes up (S18);
3. its own writes and commit; the flags go after the commit or the rollback,
   still under the guard (``release``).

``release`` / ``acquire`` let a door leave the guard while it reads the
network (the Home Assistant price) and take it again before its next anchor.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.services import spoolman_source

logger = logging.getLogger(__name__)

SOURCE_KEYS = frozenset({"spoolman_enabled", "spoolman_url"})

SOURCE_CHANGED = "spoolman_source_changed"

SPOOLMAN_SOURCE_MESSAGES = {
    spoolman_source.CHARGE_WINDOW: "A print is being charged to Spoolman right now. Try again in a moment",
    spoolman_source.PRINT_ACCOUNTING: (
        "While a print is accounted in Spoolman, the Spoolman address or database cannot be changed. "
        "Try again after the print finishes"
    ),
    spoolman_source.SLOT_WRITES: "Spools are being assigned to slots right now. Try again in a moment",
    SOURCE_CHANGED: "Spoolman settings or its history are changing right now. Try again",
}

# How long a change of the mode waits for the slot-ledger writers in flight (acceptance A08):
# their lanes and a commit, never a print — past it the change is refused, to be retried.
SLOT_WRITES_WAIT_SECONDS = 10.0
_SLOT_WRITES_POLL_SECONDS = 0.02


# --- a consistent snapshot of the source (S0) ---------------------------------------


@dataclass(frozen=True)
class SourceSnapshot:
    """The three rows as one committed state, with the generations they belong to."""

    generation: int
    base_generation: int
    enabled: bool
    url: str
    record: dict = field(default_factory=dict)


class SourceUnsettled(Exception):
    """A change of the source ran while the snapshot was read; the caller retries."""


async def read_source_rows() -> tuple[dict, dict]:
    """``spoolman_enabled`` / ``spoolman_url`` / the history-source record, read in one session."""
    from backend.app.core import database  # noqa: PLC0415
    from backend.app.models.settings import Settings  # noqa: PLC0415
    from backend.app.services.spoolman_usage_history import SOURCE_SETTING  # noqa: PLC0415

    keys = ("spoolman_enabled", "spoolman_url", SOURCE_SETTING)
    async with database.async_session() as db:
        rows = dict((await db.execute(select(Settings.key, Settings.value).where(Settings.key.in_(keys)))).all())
    try:
        record = json.loads(rows.get(SOURCE_SETTING) or "")
    except (TypeError, ValueError):
        record = {}
    if not isinstance(record, dict) or not isinstance(record.get("token"), str) or not record.get("token"):
        record = {}
    return rows, record


async def settled_snapshot() -> SourceSnapshot:
    """S0: the rows read while no change of the source runs (spec §C1, S21, S28; acceptance A06).

    A change holds the source guard from its first read to its commit and raises the
    generation on the way, so a free guard before and after the read with the same
    generation proves the rows are one committed state of that generation. Otherwise
    :class:`SourceUnsettled` — never a mix of an old row and a new generation.
    """
    if not spoolman_source.guard_free():
        raise SourceUnsettled
    generation = spoolman_source.generation()
    base_generation = spoolman_source.base_generation()
    rows, record = await read_source_rows()
    if spoolman_source.generation() != generation or not spoolman_source.guard_free():
        raise SourceUnsettled
    return SourceSnapshot(
        generation=generation,
        base_generation=base_generation,
        enabled=(rows.get("spoolman_enabled") or "").strip().lower() == "true",
        url=(rows.get("spoolman_url") or "").strip(),
        record=record,
    )


def source_changed() -> HTTPException:
    """409 ``spoolman_source_changed``: the source moved under a door's proof — try again."""
    return HTTPException(
        status_code=409, detail={"error": SOURCE_CHANGED, "message": SPOOLMAN_SOURCE_MESSAGES[SOURCE_CHANGED]}
    )


def require_unchanged(snapshot: SourceSnapshot) -> None:
    """After a door's remote proof and its admission: the source is still the snapshot's, or 409.

    Called right after ``spoolman_activity.slot_write`` and ``first_evidence`` are
    entered: from there no change of the database can start and a change of the mode
    waits for the door, so a generation still equal to S0's means the proof belongs to
    the source the door writes for until its commit.
    """
    if spoolman_source.generation() != snapshot.generation:
        raise source_changed()


@contextlib.asynccontextmanager
async def admitted_for(snapshot: SourceSnapshot, kind: str):
    """A slot-ledger door's admission for a proof taken under ``snapshot`` (acceptance A06, A08).

    The first context of a door's ``async with``: the slot-ledger write, then the
    first-evidence admission (A10), both waited for before any lane, and the proof's
    source checked right after them — before the lane too, and valid until the door's
    commit.
    """
    from backend.app.services import spoolman_activity  # noqa: PLC0415

    async with spoolman_activity.slot_write(kind), spoolman_activity.first_evidence(kind):
        require_unchanged(snapshot)
        yield


def refusal(error: spoolman_source.SourceRefused) -> HTTPException:
    """409 ``spoolman_print_in_progress`` with its reason (S43)."""
    return HTTPException(
        status_code=409,
        detail={
            "error": "spoolman_print_in_progress",
            "reason": error.reason,
            "message": SPOOLMAN_SOURCE_MESSAGES[error.reason],
        },
    )


def refusal_message(error: spoolman_source.SourceRefused) -> str:
    return SPOOLMAN_SOURCE_MESSAGES[error.reason]


def names_source(keys) -> bool:
    return bool(SOURCE_KEYS & set(keys))


async def mode_on(db: AsyncSession) -> bool:
    """Whether Spoolman is the inventory now — the row as committed, read as a column.

    A slot-ledger door asks it again under its record (acceptance A08). A column read,
    not an ORM object: a session hands back an object it still holds with the value it
    loaded (``expire_on_commit=False``). No door keeps that object today, so
    ``get_setting`` would read fresh too — the column read does not depend on it.
    """
    from backend.app.models.settings import Settings  # noqa: PLC0415

    value = await db.scalar(select(Settings.value).where(Settings.key == "spoolman_enabled"))
    return _enabled(value)


def _normalized(url: str | None) -> str:
    from backend.app.services.spoolman_usage_history import normalize_url  # noqa: PLC0415

    return normalize_url(url)


def _enabled(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() == "true"


async def protected_print_exists(db: AsyncSession) -> bool:
    """The durable predicate (S38): a print that accounts in Spoolman — ONE statement, a fresh snapshot.

    An archive ``printing`` while Spoolman mode is on, or an archive
    ``printing`` with a tracking row or a journal event naming a Spoolman
    spool, whatever the mode. Read only under the base-change flag (S44).
    """
    from backend.app.models.active_print_spoolman import ActivePrintSpoolman  # noqa: PLC0415
    from backend.app.models.archive import PrintArchive  # noqa: PLC0415
    from backend.app.models.print_usage_event import PrintUsageEvent  # noqa: PLC0415
    from backend.app.models.settings import Settings  # noqa: PLC0415

    mode_on = select(func.lower(Settings.value)).where(Settings.key == "spoolman_enabled").scalar_subquery() == "true"
    tracking = exists().where(ActivePrintSpoolman.archive_id == PrintArchive.id)
    journal = exists().where(
        PrintUsageEvent.archive_id == PrintArchive.id, PrintUsageEvent.spoolman_spool_id.is_not(None)
    )
    statement = (
        select(PrintArchive.id).where(PrintArchive.status == "printing").where(mode_on | tracking | journal).limit(1)
    )
    return (await db.execute(statement)).first() is not None


class SourceChange:
    """One door's ownership of a possible change of the Spoolman source."""

    def __init__(self) -> None:
        self._stack: contextlib.AsyncExitStack | None = None
        self._base = False
        self._mode = False
        self.changed = False

    @property
    def held(self) -> bool:
        return self._stack is not None

    async def acquire(self, db: AsyncSession) -> None:
        if self._stack is not None:
            return
        stack = contextlib.AsyncExitStack()
        await stack.enter_async_context(spoolman_source.guard(db))
        self._stack = stack

    async def release(self) -> None:
        if self._base:
            spoolman_source.end_base_change()
            self._base = False
        if self._mode:
            spoolman_source.end_mode_change()
            self._mode = False
        stack, self._stack = self._stack, None
        if stack is not None:
            await stack.aclose()

    async def decide(
        self,
        db: AsyncSession,
        *,
        enabled=None,
        url=None,
        rotate: bool = False,
        record_changes: bool = False,
    ) -> bool:
        """Classify the change against the current rows and apply the refusals. Returns ``changed``.

        ``enabled`` / ``url`` — the new values the door writes (None: not
        written); ``rotate`` — a new database behind the same address;
        ``record_changes`` — the history-source record itself changes (reset, "same database").
        """
        from backend.app.api.routes.settings import get_setting  # noqa: PLC0415

        spoolman_source.require_held()
        current_enabled = _enabled(await get_setting(db, "spoolman_enabled"))
        current_url = _normalized(await get_setting(db, "spoolman_url"))
        new_url = _normalized(url) if url is not None else current_url
        mode_changes = enabled is not None and _enabled(enabled) != current_enabled
        url_changes = url is not None and new_url != current_url
        base = rotate or (url_changes and bool(new_url))
        self.changed = bool(mode_changes or url_changes or rotate or record_changes)
        if not self.changed:
            return False
        try:
            spoolman_source.refuse_during_charge()
            if mode_changes:
                # Before a base change's credential anchor: the writers waited for need the writer.
                await self._wait_for_slot_writers(db)
            if base:
                # The flag is set only while this transaction holds SQLite's writer (the credential
                # anchor, idempotent here): a first-evidence writer waiting for the flag can then
                # never be the one holding the writer this change needs (no wait cycle).
                from backend.app.core.credential_config import lock_credential_config  # noqa: PLC0415

                await lock_credential_config(db)
                spoolman_source.begin_base_change()
                self._base = True
                if await protected_print_exists(db):
                    raise spoolman_source.SourceRefused(spoolman_source.PRINT_ACCOUNTING)
                spoolman_source.bump_base_generation()
                # The numbers of every binding and K link are the database's being left.
                from backend.app.services import spoolman_spool_identity  # noqa: PLC0415

                await spoolman_spool_identity.clear_all(db)
            if mode_changes:
                await clear_left_ledger(db, spoolman=_enabled(enabled))
                # A print may have finished while this change waited: checked again, and the
                # generation raised in the same synchronous step.
                spoolman_source.refuse_during_charge()
        except spoolman_source.SourceRefused as exc:
            raise refusal(exc) from exc
        if mode_changes or (base and current_enabled):
            # After the last refusal (acceptance A11): a refused change rolls its rows back and
            # keeps the overlay that describes them. A base change empties the Spoolman ledger,
            # which the overlay describes only while Spoolman is the inventory.
            _forget_the_overlay()
        spoolman_source.bump_generation()
        return True

    async def wait_before_anchor(self, db: AsyncSession, *, enabled) -> None:
        """A change of the mode waits for the slot-ledger writers before the door's anchor (A12).

        The general settings save, the reset and the Git settings restore take the credential
        anchor — SQLite's writer — before :meth:`decide`, and a door holding it cannot wait
        for writers that may need it. They call this first, under the guard in their clean
        session, with the mode they will write: the flag is set and the writers in flight
        finish here, as they do inside ``decide`` for the Spoolman settings route. ``decide``
        then finds none open. No mode change → nothing.

        What a door refuses without its anchor it refuses before this (acceptance A14): the
        general save asks the caller's role first, the reset is admin-only at its dependency,
        a Git run proves its actor before its first category. A refusal decided only under
        the anchor — the save's effective settings, an actor revoked during a Git run — comes
        after the wait: bounded by it, nothing written, the flag dropped at release.
        """
        spoolman_source.require_held()
        if enabled is None or self._mode or _enabled(enabled) == await mode_on(db):
            return
        try:
            spoolman_source.refuse_during_charge()
            await self._wait_for_slot_writers(db)
        except spoolman_source.SourceRefused as exc:
            raise refusal(exc) from exc

    async def _wait_for_slot_writers(self, db: AsyncSession) -> None:
        """The mode-change flag, then the slot-ledger writers already in flight finish (A08).

        New writers wait for the flag before their lanes. A door that already holds
        SQLite's writer or a serialising row lock cannot wait for writers that may need
        it: with one in flight it is refused at once — the doors that take their anchor
        first wait in :meth:`wait_before_anchor` instead, so this stays a safety net. A
        print that finishes meanwhile refuses the change as at its start.
        """
        from backend.app.core.database import writer_reserved  # noqa: PLC0415
        from backend.app.services import spoolman_activity  # noqa: PLC0415

        spoolman_source.begin_mode_change()
        self._mode = True
        if not spoolman_activity.slot_writes_open():
            return
        if writer_reserved(db):
            raise spoolman_source.SourceRefused(spoolman_source.SLOT_WRITES)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SLOT_WRITES_WAIT_SECONDS
        while spoolman_activity.slot_writes_open():
            if loop.time() >= deadline:
                raise spoolman_source.SourceRefused(spoolman_source.SLOT_WRITES)
            await asyncio.sleep(_SLOT_WRITES_POLL_SECONDS)
            spoolman_source.refuse_during_charge()


async def clear_left_ledger(db: AsyncSession, *, spoolman: bool) -> int:
    """Empty the slot ledger of the mode being left, in the change's transaction (acceptance A08).

    The ledgers hold the active mode only — no reader gates on the mode — so every door
    that moves it clears the other one: ``spool_assignments`` on the switch to
    Spoolman, ``spoolman_slot_assignments`` on the switch back (#1473). Only a
    :class:`SourceChange` calls it, after the slot-ledger writers in flight finished;
    the change forgets the overlay once it can no longer be refused (A11).
    """
    from backend.app.models.spool_assignment import SpoolAssignment  # noqa: PLC0415
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment  # noqa: PLC0415

    # Two literal statements, one per ledger: the content-delete census names each site.
    if spoolman:
        result = await db.execute(delete(SpoolAssignment))
    else:
        result = await db.execute(delete(SpoolmanSlotAssignment))
    if spoolman:
        logger.info("Cleared %d spool assignments on switch to Spoolman mode", result.rowcount)
    else:
        logger.info("Cleared %d Spoolman slot assignments on switch to internal mode", result.rowcount)
    return int(result.rowcount or 0)


def _forget_the_overlay() -> None:
    """Every overlay entry was born of an assignment row; with the ledger emptied, so is every
    spool the masks stood for.

    Forgotten after the change's last refusal and before its commit, like every ``remember``
    on the assignment routes. A store that still claimed a spool whose row is gone would
    route by it; one emptied by a commit that then fails shows the advertised profiles until
    the slot's next ``remember`` or a restart (``refresh_overlay``) — the lesser fault.
    """
    from backend.app.services import ams_advertised_overlay as overlay  # noqa: PLC0415

    overlay.forget_all()


@contextlib.asynccontextmanager
async def owning(db: AsyncSession, *, when: bool = True):
    """Hold a :class:`SourceChange` for a door's whole write; released after its commit or rollback."""
    change = SourceChange()
    if when:
        await change.acquire(db)
    try:
        yield change
    except BaseException:
        # The flag goes only after a proven rollback (S44): end the door's transaction first.
        if change.held:
            await db.rollback()
        raise
    finally:
        await change.release()
