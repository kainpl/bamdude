"""A deleted printer's number is never handed out again (P-ID).

SQLite without ``AUTOINCREMENT`` gives the next row ``max(id) + 1``: deleting the
newest printer gave its number to the next one, and the process memory keyed by
printer number — dicts and sets far too many to list — then belonged to the new
printer (GH109: a deleted printer's "current print" blocked the new one's first
dispatch). ``printer_deletion._ID_KEYED`` chased those containers one by one; this
removes the cause instead, as C047 (m261) did for spools. Archiving stays the
normal way out; hard delete stays for a printer added by mistake.

SQLite — ``sqlite_owns_transaction``: the runner hands ``upgrade_sqlite`` one
connection with no transaction open, and the rebuild owns ``BEGIN IMMEDIATE``,
``PRAGMA foreign_keys`` and its restore (``_sqlite_autoincrement``). The counter
starts at the bound of ``core/printer_id_bound.py``: every number a current reader
could still pick up — child rows, queues, API-key scopes, order-plan targets, AMS
position keys, settings — not just ``MAX(printers.id)``. With no number left up to
2³¹ − 1 the rebuild stops before ``COMMIT`` and leaves everything as it was. A rerun
after a commit whose marker was not written — and the DEBUG replay — only raise
the counter.

PostgreSQL — under ``LOCK TABLE … SHARE ROW EXCLUSIVE`` the sequence is first asked
whether its next number is still allowed (``min(seqmax, 2³¹ − 1)``), with or without
any carrier, then raised to the same bound, only upward (``core/pg_sequences.py``),
and only for a plain sequence: increment 1, ``CACHE 1``, ``NO CYCLE``.
"""

from sqlalchemy import text

from backend.app.core import pg_sequences, printer_id_bound
from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations._sqlite_autoincrement import ensure_autoincrement

version = 262
name = "printer_id_never_reissued"
sqlite_owns_transaction = True


def _sqlite_bound(conn):
    """The callback the rebuild calls under its ``BEGIN IMMEDIATE``: the registry's bound,
    refused when neither it nor SQLite's own counter leaves a number in the domain."""
    bound = printer_id_bound.sqlite_bound(conn, printer_id_bound.sqlite_applied_versions(conn))
    printer_id_bound.require_next_number(bound, printer_id_bound.sqlite_allocator(conn))
    return bound


def upgrade_sqlite(dbapi_connection, *, fault=None):
    """The SQLite arm: called by the runner for the pending run and the DEBUG replay alike."""
    return ensure_autoincrement(dbapi_connection, "printers", _sqlite_bound, fault=fault)


def require_next_postgres_number(state: pg_sequences.SequenceState, bound: int | None) -> None:
    """Stop when the sequence's next number, or the first one above the bound, lies past the
    ceiling — asked of the allocator itself, not only of the carriers (PID-S25)."""
    ceiling = min(state.maximum, printer_id_bound.MAX_PRINTER_ID)
    if state.next_value > ceiling or (bound is not None and bound >= ceiling):
        raise pg_sequences.SequenceError(
            f"printers: the id sequence is exhausted (bound {bound}, next {state.next_value}, ceiling {ceiling})"
        )


async def upgrade(conn):
    """The PostgreSQL arm, inside the runner's transaction. SQLite never comes here."""
    if is_sqlite():
        raise RuntimeError("m262 owns its SQLite transaction: the runner calls upgrade_sqlite")
    # Migrations run before any request is admitted; the lock is a safeguard, and the
    # timeout keeps a stray external writer from holding the start up without a word.
    await conn.execute(text("SET LOCAL lock_timeout = '30s'"))
    await conn.execute(text("LOCK TABLE printers IN SHARE ROW EXCLUSIVE MODE"))
    applied = set((await conn.execute(text("SELECT version FROM _migrations"))).scalars())
    bound = await printer_id_bound.postgres_bound(conn, applied)
    state = await pg_sequences.sequence_state(conn, "printers")
    if state is None:
        raise pg_sequences.SequenceError("printers: id owns no sequence")
    require_next_postgres_number(state, bound)
    await pg_sequences.raise_to(conn, "printers", bound, plain=True, required=True)


replay = upgrade
