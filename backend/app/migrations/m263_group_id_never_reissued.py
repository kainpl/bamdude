"""A deleted group's number is never handed out again (N07 spec §B).

SQLite without ``AUTOINCREMENT`` gives the next row ``max(id) + 1``: deleting the
newest group gave its number to the next one, and an OIDC provider's default group,
a Telegram chat's group or a membership row SQLite never cascaded then named the new
group. N07's OIDC group mapping stores group numbers, so the same reuse would put
people into the wrong group. This makes ``groups`` an ``AUTOINCREMENT`` table, as
m261 did for spools and m262 for printers.

SQLite — ``sqlite_owns_transaction``: the runner hands ``upgrade_sqlite`` one
connection with no transaction open, and the rebuild owns ``BEGIN IMMEDIATE``,
``PRAGMA foreign_keys`` and its restore (``_sqlite_autoincrement``). The counter
starts at the bound of ``core/group_id_bound.py`` — every number a current reader
could still pick up, not just ``MAX(groups.id)``. The allocator is asked on its own as
well: with no number left up to 2³¹ − 1 after the bound, ``MAX(groups.id)`` or a
retained ``sqlite_sequence`` row, the rebuild stops before ``COMMIT`` and leaves
everything as it was. A rerun after a commit whose marker was not written — and the
DEBUG replay — only raise the counter.

PostgreSQL — under ``LOCK TABLE … SHARE ROW EXCLUSIVE`` the sequence is first asked
whether its next number is still allowed (``min(seqmax, 2³¹ − 1)``), with or without
any carrier, then raised to the same bound, only upward (``core/pg_sequences.py``),
and only for a plain sequence: increment 1, ``CACHE 1``, ``NO CYCLE``.
"""

from sqlalchemy import text

from backend.app.core import group_id_bound, id_allocator, pg_sequences
from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations._sqlite_autoincrement import ensure_autoincrement

version = 263
name = "group_id_never_reissued"
sqlite_owns_transaction = True


def _sqlite_bound(conn):
    """The callback the rebuild calls under its ``BEGIN IMMEDIATE``: the registry's bound,
    refused when neither it nor SQLite's own counter leaves a number in the domain."""
    bound = group_id_bound.sqlite_bound(conn, group_id_bound.sqlite_applied_versions(conn))
    group_id_bound.require_next_number(bound, group_id_bound.sqlite_allocator(conn))
    return bound


def upgrade_sqlite(dbapi_connection, *, fault=None):
    """The SQLite arm: called by the runner for the pending run and the DEBUG replay alike."""
    return ensure_autoincrement(dbapi_connection, "groups", _sqlite_bound, fault=fault)


async def upgrade(conn):
    """The PostgreSQL arm, inside the runner's transaction. SQLite never comes here."""
    if is_sqlite():
        raise RuntimeError("m263 owns its SQLite transaction: the runner calls upgrade_sqlite")
    await conn.execute(text("SET LOCAL lock_timeout = '30s'"))
    await conn.execute(text("LOCK TABLE groups IN SHARE ROW EXCLUSIVE MODE"))
    applied = set((await conn.execute(text("SELECT version FROM _migrations"))).scalars())
    bound = await group_id_bound.postgres_bound(conn, applied)
    state = await pg_sequences.sequence_state(conn, "groups")
    if state is None:
        raise pg_sequences.SequenceError("groups: id owns no sequence")
    id_allocator.require_next_postgres_number(state, bound, "groups")
    await pg_sequences.raise_to(conn, "groups", bound, plain=True, required=True)


replay = upgrade
