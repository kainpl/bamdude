"""Record which execution a persisted usage session belongs to.

``active_print_sessions.archive_identity`` holds the ``recovery_identity`` of the archive the session's start
bound. A name cannot carry that proof: after a power cycle an A1 mini reports ``subtask_name`` as the file
name (``X.3mf`` for the ``X`` it echoed at start), printers fold spaces into underscores, and two different
prints can share a folded, truncated or legacy name (GH #115, Codex review USN-1).

Existing rows stay NULL. NULL means "written before this column, or never claimed by an archive", and it keeps
the old rule: the name exactly as recorded. No backfill: nothing in an existing row proves its execution.
"""

from backend.app.migrations.helpers import add_column

version = 221
name = "active_print_session_owner"


async def upgrade(conn):
    await add_column(conn, "active_print_sessions", "archive_identity VARCHAR(36)")
