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

Process memory only; one application process (spec §C6).
"""

from __future__ import annotations

import contextlib
import logging
from collections import Counter

logger = logging.getLogger(__name__)

WINDOW = "accounting_window"


class _Activity:
    def __init__(self) -> None:
        self.revision = 0
        self.open: Counter[str] = Counter()

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


@contextlib.asynccontextmanager
async def first_evidence(kind: str):
    """A writer of a print's first Spoolman evidence (S41, S44).

    Waits — outside SQLite's writer, with no uncommitted writes — until no change
    of the Spoolman database is in flight, then in ONE synchronous step checks
    that again and publishes its record. Whatever it read before this point it
    must prove again inside. The record lasts until the body ends (its commit or
    rollback); from then on the durable predicate (an archive ``printing``) keeps
    the database from changing.
    """
    from backend.app.services import spoolman_source  # noqa: PLC0415

    while True:
        await spoolman_source.wait_no_base_change()
        if not spoolman_source.base_change_in_flight():
            _activity._enter(kind)
            break
    try:
        yield
    finally:
        _activity._leave(kind)


def reset_for_tests() -> None:
    _activity.revision = 0
    _activity.open.clear()
