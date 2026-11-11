"""Record which execution a persisted usage session belongs to.

A name cannot carry that proof. After a power cycle an A1 mini reports ``subtask_name`` as the file name
(``X.3mf`` for the ``X`` it echoed at start), printers fold spaces into underscores, and two different prints
can share a folded, truncated or legacy name (GH #115, Codex review USN-1). Two columns on
``active_print_sessions`` carry the proof:

- ``archive_identity`` is the ``recovery_identity`` of the archive the session's start bound;
- ``owner_tracked`` is true on every row an owner-aware start writes. With no ``archive_identity`` such a row
  is a start no archive has claimed yet, so it proves nothing.

Existing rows stay NULL in both columns. Those, and only those, keep the old rule: the name exactly as recorded.
No backfill, because nothing in an existing row proves its execution.
"""

from backend.app.migrations.helpers import add_column

version = 221
name = "active_print_session_owner"


async def upgrade(conn):
    await add_column(conn, "active_print_sessions", "archive_identity VARCHAR(36)")
    await add_column(conn, "active_print_sessions", "owner_tracked BOOLEAN")
