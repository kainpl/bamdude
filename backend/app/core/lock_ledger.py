"""What the current transaction holds locked (WS-13 E1, spec BL0 / BL2).

Deadlock freedom rests on one rule: a door that WAITS on row locks takes them in
one order of classes (below) and never a lower class after a higher one. To keep
that rule a door must know what its own transaction already holds — a position
the issue dialog locked for every line is not locked again after a line, and a
position that appeared since is a refusal, not a late lock (spec BL6).

The ledger belongs to the session's OUTER transaction, not to the session and not
to single commit/rollback events: SQLAlchemy fires ``after_commit`` and
``after_rollback`` for a SAVEPOINT too. Rules (spec BL2, (1)–(4)):

1. the outer transaction ending — commit or rollback — drops the ledger whole;
   the session's next transaction starts empty;
2. a SAVEPOINT beginning snapshots the ledger;
3. a SAVEPOINT released keeps everything, before and inside it;
4. a SAVEPOINT rolled back restores the snapshot: PostgreSQL released the locks
   taken inside it, the ones taken before it stay held.

Event order the listeners rely on (measured on SQLAlchemy 2.0): a released
savepoint fires ``after_transaction_end``; a rolled-back one fires
``after_transaction_end`` and THEN ``after_soft_rollback`` naming it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from sqlalchemy import event
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# spec BL0 — the classes a door waits on, in the only order it may take them.
LIBRARY_FILE = 1
PRODUCT = 2
ORDER = 3
POSITION = 4
LINE = 5
# A print (``print_archives``) — after the order it leaves or joins and that order's lines
# (WS-13 E13 V04): the exits lock the order before they read C1, as receiving does, and
# only then the print; a print's writers go on to the parts' ledger after it.
PRINT = 6
PART = 7
PROCUREMENT = 8

_KEY = "lock_ledger"

#: Strict mode — the order monitor raises instead of logging (spec BL2). The test
#: suite turns it on (``conftest``); the PostgreSQL scenario runner inherits it through
#: the environment. The product gate's own preconditions do not depend on it.
STRICT = os.environ.get("BAMDUDE_LOCK_ORDER_STRICT") == "1"

_reported: set[tuple[str, int, int]] = set()


class GateOrderError(RuntimeError):
    """A programmer's error against the lock protocol (spec BL2): a door asked for a
    lock the protocol forbids at that point. Never a domain refusal — the request
    ends in a 500 and rolls back."""


@dataclass
class LockLedger:
    """``gates`` — product ids behind the product gate; ``lock_class`` — the highest
    class taken WITH waiting; ``locked_rows`` — ``(table, id)`` pairs held;
    ``created_rows`` — rows this transaction INSERTED. Nobody else can see such a
    row, let alone wait for it, so its lock never takes part in a cycle and does not
    count toward the order (a new order line locked before the position it reserves
    from, in one intake)."""

    gates: set[int] = field(default_factory=set)
    lock_class: int = 0
    locked_rows: set[tuple[str, int]] = field(default_factory=set)
    created_rows: set[tuple[str, int]] = field(default_factory=set)
    _stack: list[tuple[object, tuple]] = field(default_factory=list, repr=False)
    _last_ended: tuple[object, tuple] | None = field(default=None, repr=False)

    def snapshot(self) -> tuple:
        return frozenset(self.gates), self.lock_class, frozenset(self.locked_rows), frozenset(self.created_rows)

    def restore(self, snapshot: tuple) -> None:
        gates, lock_class, rows, created = snapshot
        self.gates = set(gates)
        self.lock_class = lock_class
        self.locked_rows = set(rows)
        self.created_rows = set(created)

    def holds(self, table: str, row_id: int) -> bool:
        return (table, row_id) in self.locked_rows

    def held(self, table: str) -> set[int]:
        return {row_id for name, row_id in self.locked_rows if name == table}

    def held_existing(self, table: str) -> set[int]:
        """Held rows of ``table`` that other transactions can see (not created here)."""
        return {
            row_id for name, row_id in self.locked_rows if name == table and (name, row_id) not in self.created_rows
        }

    def note(self, table: str, row_id: int, lock_class: int) -> None:
        """A row lock taken WITH waiting — the class counts toward the order, unless
        the row is this transaction's own new one."""
        self.locked_rows.add((table, row_id))
        if (table, row_id) not in self.created_rows:
            self.lock_class = max(self.lock_class, lock_class)

    def note_created(self, table: str, row_id: int) -> None:
        self.created_rows.add((table, row_id))


def before_lock(db, table: str, row_id: int, lock_class: int) -> bool:
    """The diagnostic monitor of the lock helpers (spec BL2): called BEFORE a lock is
    taken with waiting. Returns False when this transaction already holds the row (a
    re-lock never waits and is not checked). A NEW row of a class lower than one
    already taken is a violation: strict mode raises :class:`GateOrderError`, a
    working process logs it once per (table, class, class) and carries on — an old
    door with an unknown order must not stop production (review round 5)."""
    held = ledger(db)
    if held.holds(table, row_id) or (table, row_id) in held.created_rows:
        return False
    if lock_class < held.lock_class:
        message = f"lock order: {table} #{row_id} (class {lock_class}) taken with waiting after a class-{held.lock_class} lock"
        if STRICT:
            raise GateOrderError(message)
        key = (table, lock_class, held.lock_class)
        if key not in _reported:
            _reported.add(key)
            logger.error(message)
    return True


def ledger(db) -> LockLedger:
    """The ledger of ``db``'s current transaction — an ``AsyncSession`` or a ``Session``."""
    info = db.info
    found = info.get(_KEY)
    if found is None:
        found = info[_KEY] = LockLedger()
    return found


# The tables whose rows the protocol locks — a row of one of them INSERTed by this
# transaction is invisible to every other, so its lock can take part in no cycle.
_TRACKED = {
    "library_files",
    "products",
    "projects",
    "stock_items",
    "project_lines",
    "print_archives",
    "product_parts",
    # G03 B, A20: a router row this transaction inserted (a rebalance companion) is not ordered by lock_auto_rows.
    "auto_queue_items",
}


@event.listens_for(Session, "after_flush")
def _rows_created(session, _flush_context) -> None:
    """Record the rows this flush INSERTed. ``session.new`` still lists them here —
    SQLAlchemy resets it after this event — and their keys are already assigned."""
    for obj in session.new:
        table = getattr(obj, "__tablename__", None)
        if table not in _TRACKED:
            continue
        row_id = getattr(obj, "id", None)
        if isinstance(row_id, int):
            ledger(session).note_created(table, row_id)


@event.listens_for(Session, "after_transaction_create")
def _transaction_began(session, transaction) -> None:
    if transaction.parent is None:
        # A ledger made just before autobegin (a door asked what it holds before its
        # first statement) belongs to this transaction; a finished one was dropped.
        session.info.setdefault(_KEY, LockLedger())
    elif transaction.nested:
        ledger(session)._stack.append((transaction, ledger(session).snapshot()))


@event.listens_for(Session, "after_transaction_end")
def _transaction_ended(session, transaction) -> None:
    if transaction.parent is None:
        session.info.pop(_KEY, None)
        return
    if not transaction.nested:
        return
    found = session.info.get(_KEY)
    if found is None or not found._stack or found._stack[-1][0] is not transaction:
        return
    # Released: keep everything. A rollback follows as ``after_soft_rollback``.
    found._last_ended = found._stack.pop()


@event.listens_for(Session, "after_soft_rollback")
def _savepoint_rolled_back(session, previous_transaction) -> None:
    if not getattr(previous_transaction, "nested", False):
        return
    found = session.info.get(_KEY)
    if found is None or found._last_ended is None or found._last_ended[0] is not previous_transaction:
        return
    found.restore(found._last_ended[1])
    found._last_ended = None
