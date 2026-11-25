"""A built-in spool's number is never handed out again (C047, adapted from upstream e1714b511).

SQLite without ``AUTOINCREMENT`` gives the next row ``max(id) + 1``: deleting the
newest spool gave its number to the next one, and a printed numeric label, a
slot row or a label job still naming it then pointed at a different spool.
Upstream solved it together with deleting USED spools (``SET NULL`` on the usage
history); BamDude keeps its rule that a used spool is archived (409
``spool_in_use``) and takes only the number half.

SQLite — ``sqlite_owns_transaction``: the runner hands ``upgrade_sqlite`` one
connection with no transaction open, and the rebuild owns ``BEGIN IMMEDIATE``,
``PRAGMA foreign_keys`` and its restore (``_sqlite_autoincrement``). The counter
starts at the bound of ``core/spool_id_bound.py``: every number the database
still remembers, not just ``MAX(spool.id)``. A rerun after a commit whose marker
was not written — and the DEBUG replay — only raise the counter.

PostgreSQL — the id sequence is raised to the same bound under ``LOCK TABLE …
SHARE ROW EXCLUSIVE``, only upward (``core/pg_sequences.py``), and only for a
plain sequence: increment 1, ``CACHE 1``, ``NO CYCLE``. Anything else stops the
migration with a diagnostic rather than leave a cached number to come back.
"""

from sqlalchemy import text

from backend.app.core import pg_sequences, spool_id_bound
from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations._sqlite_autoincrement import ensure_autoincrement

version = 261
name = "spool_id_never_reissued"
sqlite_owns_transaction = True


def _sqlite_bound(conn):
    return spool_id_bound.sqlite_bound(conn, spool_id_bound.sqlite_applied_versions(conn))


def upgrade_sqlite(dbapi_connection, *, fault=None):
    """The SQLite arm: called by the runner for the pending run and the DEBUG replay alike."""
    return ensure_autoincrement(dbapi_connection, "spool", _sqlite_bound, fault=fault)


async def upgrade(conn):
    """The PostgreSQL arm, inside the runner's transaction. SQLite never comes here."""
    if is_sqlite():
        raise RuntimeError("m261 owns its SQLite transaction: the runner calls upgrade_sqlite")
    # Migrations run before any request is admitted; the lock is a safeguard, and the
    # timeout keeps a stray external writer from holding the start up without a word.
    await conn.execute(text("SET LOCAL lock_timeout = '30s'"))
    await conn.execute(text("LOCK TABLE spool IN SHARE ROW EXCLUSIVE MODE"))
    applied = set((await conn.execute(text("SELECT version FROM _migrations"))).scalars())
    bound = await spool_id_bound.postgres_bound(conn, applied)
    await pg_sequences.raise_to(conn, "spool", bound, plain=True, required=True)


replay = upgrade
