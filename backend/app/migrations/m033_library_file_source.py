"""Add ``library_files.source_type`` + ``library_files.source_url`` for B.5
(MakerWorld import) and B.4 (server-side slicing) provenance tracking.

Both features need to record where a library file came from so the UI can
badge it ("imported from MakerWorld", "sliced via OrcaSlicer") and so we can
dedupe re-imports of the same MakerWorld plate by canonical URL. Indexed on
``source_url`` to keep dedup lookups O(log N) as the library grows.

Existing rows are NULL on both columns and continue to render exactly as
before — only future MakerWorld imports + slicer outputs populate them.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column

version = 33
name = "library_file_source"


async def upgrade(conn):
    await add_column(conn, "library_files", "source_type VARCHAR(32)")
    await add_column(conn, "library_files", "source_url VARCHAR(512)")

    # Idempotent index creation — both backends support `CREATE INDEX IF NOT EXISTS`.
    if is_postgres():
        already = await conn.execute(text("SELECT 1 FROM pg_indexes WHERE indexname='ix_library_files_source_url'"))
    else:
        already = await conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_library_files_source_url'")
        )
    if not already.scalar():
        await conn.execute(text("CREATE INDEX ix_library_files_source_url ON library_files(source_url)"))


async def seed(session_factory):  # pragma: no cover — no-op
    async with session_factory() as db:
        _ = db  # noqa: ARG001
