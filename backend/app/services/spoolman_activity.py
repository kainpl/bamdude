"""What a Spoolman delete must not race: print accounting and the first facts of a print (C047 part 2).

Spec ``60-specs/c047-spool-id-never-reissued-spec.md`` (vault), §C4 and §C1.

A delete of a Spoolman spool reads local facts — usage history, slot rows,
tracking rows, journal events — and only then admits itself. Something that
can make one of those facts say "in use" may change between the read and the
admission. Every such writer publishes an **activity record** here for its
whole life, from entry to its own commit or rollback, and every record raises
one monotonic **revision**. The delete takes the revision (A0) right before its
reads and admits only when the revision is still A0 and nothing is open — so a
change that came and went completely between the two reads is seen as well.

Three kinds of record:

- the **accounting window** — the first line of ``main.on_print_complete`` to
  its ``finally``; it never waits (S15);
- an ordinary record — Git's SPOOLS category restoring usage history;
- a **first-evidence record** — a print going to ``printing`` (start,
  adoption, replay), a tracking row, a journal event with a Spoolman spool id.
  These also guard the Spoolman *database*: before publishing they wait until
  no base change is in flight (``spoolman_source.base_change_in_flight``), and
  a base change refuses while one is open (S41, S44).

Apart from them, a **slot-ledger write** (acceptance A08): a door that writes a
row of ``spool_assignments`` or ``spoolman_slot_assignments`` holds one from
before its mode check to its commit. A change of the mode waits for those in
flight before it clears the ledger of the mode it leaves, and new ones wait for
the change. They raise no revision and no delete or base change sees them — a
slot row's Spoolman number is the binding fence's business. A door takes it
BEFORE its first-evidence admission (A10): one waiting for a change of the mode
holds no record that change's new address would take for a print's.

Process memory only; one application process (spec §C6).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from collections import Counter
from collections.abc import Callable

logger = logging.getLogger(__name__)

WINDOW = "accounting_window"

# The task whose first-evidence record is open in this context: a nested entry by the SAME task
# (a lane holder that took its admission before the lane, then the journal lock inside) reuses
# it. A child task inherits the variable but is another task, so it is admitted on its own.
_evidence_task: contextvars.ContextVar[asyncio.Task | None] = contextvars.ContextVar(
    "spoolman_first_evidence_task", default=None
)
# The same for a slot-ledger write: a door's helper that writes the row inside the door's own
# record takes no second one.
_slot_task: contextvars.ContextVar[asyncio.Task | None] = contextvars.ContextVar(
    "spoolman_slot_write_task", default=None
)


class _Activity:
    def __init__(self) -> None:
        self.revision = 0
        self.open: Counter[str] = Counter()
        self.slot_writes: Counter[str] = Counter()

    def _enter(self, kind: str) -> None:
        self.revision += 1
        self.open[kind] += 1

    def _leave(self, kind: str) -> None:
        self.revision += 1
        self.open[kind] -= 1
        if self.open[kind] <= 0:
            del self.open[kind]

    def window_open(self) -> bool:
        return self.open.get(WINDOW, 0) > 0

    def records_open(self) -> bool:
        """An open record other than the accounting window."""
        return any(count > 0 for kind, count in self.open.items() if kind != WINDOW)

    def anything_open(self) -> bool:
        return bool(self.open)


_activity = _Activity()


def revision() -> int:
    return _activity.revision


def window_open() -> bool:
    return _activity.window_open()


def records_open() -> bool:
    return _activity.records_open()


def anything_open() -> bool:
    return _activity.anything_open()


def enter_window() -> None:
    """Open the accounting window — synchronous, the first statement of the completion callback."""
    _activity._enter(WINDOW)


def leave_window() -> None:
    _activity._leave(WINDOW)


@contextlib.contextmanager
def record(kind: str):
    """An ordinary activity record: published on entry, before the first yield of the ``with`` body."""
    _activity._enter(kind)
    try:
        yield
    finally:
        _activity._leave(kind)


@contextlib.asynccontextmanager
async def arecord(kind: str):
    """:func:`record` for an ``async with`` that also takes async locks (published on entry)."""
    with record(kind):
        yield


def hold(kind: str) -> Callable[[], None]:
    """An activity record whose end is not a ``with`` — work handed to a task of its own.

    Published now, synchronously; the returned release ends it, once (later calls do
    nothing). The caller publishes it while its own record is still open, so the
    protection never lapses between them, and releases it when the task is done —
    from ``add_done_callback``, which also runs for a task cancelled before it started.
    """
    _activity._enter(kind)
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            _activity._leave(kind)

    return release


def evidence_held() -> bool:
    """This task holds a first-evidence record (see :func:`first_evidence`)."""
    task = asyncio.current_task()
    return task is not None and _evidence_task.get() is task


_TX_ADMISSION = "c047_spoolman_admission"
_TX_LISTENER = "c047_spoolman_admission_listener"


def admit_for_transaction(db, kind: str) -> bool:
    """The right to write a Spoolman fact into ``db``'s transaction, asked WITHOUT waiting.

    The shared journal writer (acceptance A07) runs inside a caller's transaction,
    possibly holding SQLite's writer, so it never waits there. True: the task already
    holds a first-evidence record, or — no base change in flight — a record is
    published now and ends with the session's outermost transaction (commit,
    rollback, close), so a base change refuses until the fact is committed or gone.
    False: a base change is in flight; the caller writes the fact without its
    Spoolman number (unknown) instead of a number the change may be leaving.
    """
    from sqlalchemy import event  # noqa: PLC0415

    from backend.app.services import spoolman_source  # noqa: PLC0415

    if evidence_held():
        return True
    if spoolman_source.base_change_in_flight():
        return False
    sync = getattr(db, "sync_session", db)
    info = getattr(sync, "info", None)
    if not isinstance(info, dict):
        return True  # not a real session (a test double): nothing to bind
    if info.get(_TX_ADMISSION) is not None:
        return True
    if not info.get(_TX_LISTENER):
        event.listen(sync, "after_transaction_end", _end_transaction_admission)
        info[_TX_LISTENER] = True
    _activity._enter(kind)
    info[_TX_ADMISSION] = kind
    return True


def _end_transaction_admission(session, transaction) -> None:
    if transaction.parent is not None:
        return  # a savepoint: the fact lives until the outermost transaction ends
    kind = session.info.pop(_TX_ADMISSION, None)
    if kind is not None:
        _activity._leave(kind)


@contextlib.asynccontextmanager
async def first_evidence(kind: str):
    """A writer of a print's first Spoolman evidence (S41, S44).

    Waits — outside SQLite's writer, with no uncommitted writes, and before the
    lanes it takes (acceptance A05) — until no change of the Spoolman database is
    in flight, then in ONE synchronous step checks that again and publishes its
    record. Whatever it read before this point it must prove again inside. The
    record lasts until the body ends (its commit or rollback); from then on the
    durable predicate (an archive ``printing``) keeps the database from changing.

    Nested in the same task (a lane holder admitted before its lane, then the
    journal lock inside), it reuses the open record: no second wait, no second record.
    """
    from backend.app.services import spoolman_source  # noqa: PLC0415

    if evidence_held():
        yield
        return
    while True:
        await spoolman_source.wait_no_base_change()
        if not spoolman_source.base_change_in_flight():
            _activity._enter(kind)
            break
    token = _evidence_task.set(asyncio.current_task())
    try:
        yield
    finally:
        _evidence_task.reset(token)
        _activity._leave(kind)


def slot_writes_open() -> bool:
    """A slot-ledger write is in flight (see :func:`slot_write`)."""
    return any(count > 0 for count in _activity.slot_writes.values())


@contextlib.asynccontextmanager
async def slot_write(kind: str):
    """A writer of a slot ledger, from before its mode check to its commit (acceptance A08).

    Waits — before the lanes it takes and with no write of its session pending — until
    no change of the mode is in flight, then in ONE synchronous step checks that again
    and publishes its record. The door checks the mode INSIDE: from then on a change
    of the mode waits for this record to end before it clears the ledger of the mode
    it leaves, so a row written here is never left behind in the other mode's table.
    Entered before the door's :func:`first_evidence` (A10).

    Nested in the same task, it reuses the open record.
    """
    from backend.app.services import spoolman_source  # noqa: PLC0415

    task = asyncio.current_task()
    if task is not None and _slot_task.get() is task:
        yield
        return
    while True:
        await spoolman_source.wait_no_mode_change()
        if not spoolman_source.mode_change_in_flight():
            _activity.slot_writes[kind] += 1
            break
    token = _slot_task.set(task)
    try:
        yield
    finally:
        _slot_task.reset(token)
        _activity.slot_writes[kind] -= 1
        if _activity.slot_writes[kind] <= 0:
            del _activity.slot_writes[kind]


def reset_for_tests() -> None:
    _activity.revision = 0
    _activity.open.clear()
    _activity.slot_writes.clear()
