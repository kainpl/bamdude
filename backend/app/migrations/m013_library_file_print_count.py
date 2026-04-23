"""LibraryFile.print_count column for usage tracking.

Adds ``library_files.print_count INTEGER NOT NULL DEFAULT 0`` so the
on_print_complete hook can increment a per-file counter on every
successful queued print (#1008 upstream 2bf397e3). Both fields existed
upstream from earlier; BamDude only carried ``last_printed_at`` (added
in an earlier cycle), so this migration backfills the missing column
on installs upgrading from v0.4.0 → next.

Existing rows default to 0 — equivalent to "never printed yet". Failed,
cancelled and aborted prints are intentionally not counted, matching the
upstream helper's gating to ``status == 'completed'``.
"""

from backend.app.migrations.helpers import add_column

version = 13
name = "library_file_print_count"


async def upgrade(conn):
    await add_column(conn, "library_files", "print_count INTEGER NOT NULL DEFAULT 0")


async def seed(session_factory):  # pragma: no cover — no-op, column is self-defaulting
    async with session_factory() as db:
        _ = db  # noqa: ARG001
