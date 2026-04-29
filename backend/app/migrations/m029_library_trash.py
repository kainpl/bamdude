"""Add `library_files.deleted_at` for B.7 (#1008) — soft-delete / trash bin.

Two-stage file deletion:
1. Users / admins soft-delete files — the row stays in ``library_files`` with
   ``deleted_at`` stamped; the bytes stay on disk so the user can restore.
2. A background sweeper hard-deletes rows (and their bytes) whose
   ``deleted_at`` is older than the configured retention window.

The column is nullable so existing rows keep their current shape on upgrade.
The index keeps the sweeper's "find rows where deleted_at < cutoff" query
cheap even on installs with many thousands of library files.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column

version = 29
name = "library_trash"


async def upgrade(conn):
    added = await add_column(conn, "library_files", "deleted_at DATETIME")
    if added:
        # Index lets the sweeper sweep cheaply even on big installs.
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_library_files_deleted_at ON library_files(deleted_at)"))


async def seed(session_factory):  # pragma: no cover — no-op
    async with session_factory() as db:
        _ = db  # noqa: ARG001
