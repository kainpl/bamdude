"""Add ``print_archives.deleted_at`` — symmetric soft-delete for archives.

Mirrors m029 for library files. Archive auto-purge (and manual delete) now go
through the trash bin instead of hard-deleting immediately, so users can
restore an archive that was purged in error and library-file hard-delete can
check "all referencing archives are also trashed" before unlinking bytes.

Sweeper hard-deletes archive rows whose ``deleted_at`` is older than the
configured archive-trash retention window (separate setting from library trash
— archives carry print metadata that may be more or less valuable to retain
depending on operation mode).

The column is nullable so existing rows keep their current shape on upgrade.
The index keeps the sweeper's "find rows where deleted_at < cutoff" query
cheap even on installs with thousands of archives.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column

version = 34
name = "archive_trash"


async def upgrade(conn):
    added = await add_column(conn, "print_archives", "deleted_at DATETIME")
    if added:
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_print_archives_deleted_at ON print_archives(deleted_at)")
        )


async def seed(session_factory):  # pragma: no cover — no-op
    async with session_factory() as db:
        _ = db  # noqa: ARG001
