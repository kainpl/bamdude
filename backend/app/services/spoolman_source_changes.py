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
   - a change of the **database** (a new non-empty address, ``rotate``) —
     the base-change flag is set in one synchronous step with the check of the
     open activity records, and only then a FRESH read of the durable
     predicate runs (S44); a protected print → ``print_accounting``;
   - the generation goes up (S18);
3. its own writes and commit; the flag goes after the commit or the rollback,
   still under the guard (``release``).

``release`` / ``acquire`` let a door leave the guard while it reads the
network (the Home Assistant price) and take it again before its next anchor.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.services import spoolman_source

SOURCE_KEYS = frozenset({"spoolman_enabled", "spoolman_url"})

SOURCE_CHANGED = "spoolman_source_changed"

SPOOLMAN_SOURCE_MESSAGES = {
    spoolman_source.CHARGE_WINDOW: "A print is being charged to Spoolman right now. Try again in a moment",
    spoolman_source.PRINT_ACCOUNTING: (
        "While a print is accounted in Spoolman, the Spoolman address or database cannot be changed. "
        "Try again after the print finishes"
    ),
    SOURCE_CHANGED: "Spoolman settings or its history are changing right now. Try again",
}


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

    Called right after ``spoolman_activity.first_evidence`` is entered: from there no
    change of the database can start, so a generation still equal to S0's means the
    proof belongs to the database the door now writes for.
    """
    if spoolman_source.generation() != snapshot.generation:
        raise source_changed()


@contextlib.asynccontextmanager
async def admitted_for(snapshot: SourceSnapshot, kind: str):
    """A first-evidence admission for a proof taken under ``snapshot`` (acceptance A06).

    The first context of a door's ``async with``: the admission is waited for before
    any lane, and the proof's source is checked right after it — before the lane too.
    """
    from backend.app.services import spoolman_activity  # noqa: PLC0415

    async with spoolman_activity.first_evidence(kind):
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
        except spoolman_source.SourceRefused as exc:
            raise refusal(exc) from exc
        spoolman_source.bump_generation()
        return True


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
