"""Bindings and tags a Spoolman delete must not race (C047 part 2, spec §C3).

Process memory, keyed by the Spoolman spool number across every source
(conservatively). Every operation is synchronous: the fence **never waits**,
it only refuses — so it can form no wait cycle with an SQL lock (r3).

State: a global monotonic revision ``R`` and, per number,

- ``fence`` — the revision of the last admission **or release** of its delete
  (S31): a ticket smaller than it was taken before that delete or during it;
- ``deleting`` — a delete holds the number now;
- ``rev`` — the revision of the last event on the number (writer entry, tag
  mark taken or released, delete admitted or released): a delete's F0;
- ``writers`` / ``marks`` — writers inside the guard, tag marks held.

**Binding writers** (``spoolman_slot_assignments``, ``spoolman_k_profile``)
take a :func:`ticket` before the first read that proves the number and
:func:`enter` before their first DML; they stay inside through commit or
rollback. **Tag mutations** take a mark around the remote call — inside a
:func:`tag_operation` that already owns the number they only check ownership
(S27, S32). A refusal is :class:`SpoolBeingDeleted` → 409 ``spool_being_deleted``.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class SpoolBeingDeleted(Exception):
    """The number is being deleted, or was deleted after the caller's proof of it."""

    def __init__(self, number: int, *, created: bool = False) -> None:
        super().__init__(number)
        self.number = number
        # The refusal came after a confirmed create of this very number (S37).
        self.created = created


@dataclass
class _Number:
    fence: int = 0
    deleting: bool = False
    rev: int = 0
    writers: int = 0
    marks: int = 0


class _Fence:
    def __init__(self) -> None:
        self.R = 0
        self.numbers: dict[int, _Number] = {}

    def at(self, number: int) -> _Number:
        entry = self.numbers.get(number)
        if entry is None:
            entry = self.numbers[number] = _Number()
        return entry

    def bump(self, entry: _Number) -> None:
        self.R += 1
        entry.rev = self.R


_fence = _Fence()


def ticket() -> int:
    """The revision a writer proves its numbers against (taken before the first proving read)."""
    return _fence.R


def _stale(entry: _Number, issued: int | None) -> bool:
    return entry.deleting or (issued is not None and entry.fence > issued)


# --- binding writers ----------------------------------------------------------


def _enter_one(number: int, issued: int) -> None:
    entry = _fence.at(number)
    if _stale(entry, issued):
        raise SpoolBeingDeleted(number)
    entry.writers += 1
    _fence.bump(entry)


def _leave_one(number: int) -> None:
    entry = _fence.at(number)
    entry.writers -= 1


@contextlib.contextmanager
def enter(numbers, issued: int):
    """Enter the guard for every number, ascending, all or none; held through commit or rollback."""
    taken: list[int] = []
    try:
        for number in sorted({int(n) for n in numbers}):
            _enter_one(number, issued)
            taken.append(number)
    except SpoolBeingDeleted:
        for number in taken:
            _leave_one(number)
        raise
    try:
        yield
    finally:
        for number in taken:
            _leave_one(number)


@contextlib.asynccontextmanager
async def entered(numbers, issued: int):
    """:func:`enter` for an ``async with`` that also takes the printer's lane locks (lane → guard → DML)."""
    with enter(numbers, issued):
        yield


# --- tag marks and operations -------------------------------------------------


def _mark(number: int, issued: int | None) -> None:
    entry = _fence.at(number)
    if _stale(entry, issued):
        raise SpoolBeingDeleted(number)
    entry.marks += 1
    _fence.bump(entry)


def _unmark(number: int) -> None:
    entry = _fence.at(number)
    entry.marks -= 1
    _fence.bump(entry)


@dataclass(eq=False)
class TagOperation:
    """Numbers one compound door owns for its whole interval (S27, S32)."""

    issued: int | None
    owned: set[int] = field(default_factory=set)

    def add(self, number: int, *, created: bool = False) -> None:
        """Take one more number into the operation — synchronous, never waits.

        ``created``: the number came back from a confirmed create (S37); its
        refusal says so.
        """
        number = int(number)
        if number in self.owned:
            return
        try:
            _mark(number, None if created else self.issued)
        except SpoolBeingDeleted as exc:
            raise SpoolBeingDeleted(number, created=created) from exc
        self.owned.add(number)

    def owns(self, number: int) -> bool:
        return int(number) in self.owned

    def release(self) -> None:
        for number in sorted(self.owned):
            _unmark(number)
        self.owned.clear()


_current: contextvars.ContextVar[TagOperation | None] = contextvars.ContextVar("spoolman_tag_operation", default=None)


def current_operation() -> TagOperation | None:
    return _current.get()


@contextlib.contextmanager
def tag_operation(numbers=(), *, issued: int | None = None):
    """Own ``numbers`` (ascending, all or none) until the ``with`` ends; more can be added on the way.

    Entry checks the ticket too: a delete admitted or finished after ``issued``
    refuses before any mutation.
    """
    operation = TagOperation(issued)
    try:
        for number in sorted({int(n) for n in numbers}):
            operation.add(number)
    except SpoolBeingDeleted:
        operation.release()
        raise
    token = _current.set(operation)
    try:
        yield operation
    finally:
        _current.reset(token)
        operation.release()


@contextlib.asynccontextmanager
async def operation(numbers=(), *, issued: int | None = None):
    """:func:`tag_operation` for an ``async with`` that also takes other async locks."""
    with tag_operation(numbers, issued=issued) as owned:
        yield owned


@contextlib.contextmanager
def ensure_operation(*, issued: int | None = None):
    """The current operation, or a new one for the ``with`` body (a door that runs on its own)."""
    operation = _current.get()
    if operation is not None:
        yield operation
        return
    with tag_operation(issued=issued) as created:
        yield created


@contextlib.contextmanager
def mutating(number: int):
    """Around ONE remote tag mutation of ``number``: owned by the operation, or marked for the call."""
    operation = _current.get()
    if operation is not None and operation.owns(number):
        yield
        return
    if operation is not None:
        # Inside an operation that has not taken this number: take it into the operation.
        operation.add(number)
        yield
        return
    _mark(int(number), None)
    try:
        yield
    finally:
        _unmark(int(number))


# --- the delete's side --------------------------------------------------------


def snapshot(number: int) -> int | None:
    """F0: the number's revision, or None when a writer, a mark or a delete holds it now."""
    entry = _fence.at(int(number))
    if entry.writers or entry.marks or entry.deleting:
        return None
    return entry.rev


def admit_delete(number: int, f0: int) -> bool:
    """Synchronous: nothing happened on the number since F0 and nothing holds it → lease the delete."""
    entry = _fence.at(int(number))
    if entry.rev != f0 or entry.writers or entry.marks or entry.deleting:
        return False
    entry.deleting = True
    _fence.bump(entry)
    entry.fence = entry.rev
    return True


def release_delete(number: int) -> None:
    """One synchronous step (S31): a new fence ``++R``, then the lease goes — whatever the outcome."""
    entry = _fence.at(int(number))
    _fence.bump(entry)
    entry.fence = entry.rev
    entry.deleting = False


def deleting(number: int) -> bool:
    return _fence.at(int(number)).deleting


def reset_for_tests() -> None:
    _fence.R = 0
    _fence.numbers.clear()
