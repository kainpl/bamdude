"""A job's lineage by immutable identities (OrcaBamDude host spec r2.2 §11 S2).

SQLite hands a deleted row's numeric id to the next row, so a numeric id never proves that a queue row or an archive
is the one a slicer connector's receipt names. The chain receipt -> AutoQueue job -> the queue row its promotion
created -> the archive of each execution is therefore carried by UUIDs that never change for a row's life:

- ``auto_queue_items.recovery_identity`` -- a new random UUID for every AutoQueue row (``print_queue`` and
  ``print_archives`` already have theirs). Existing rows get a separate UUID each, then a unique index.
- ``print_queue.source_auto_identity`` -- the identity of the AutoQueue row a promotion created this row from.
- ``print_archives.queue_item_identity`` / ``source_auto_identity`` -- copied from the queue row a dispatch launched,
  in the transaction that creates the archive.

The lineage columns stay empty for history: they are never back-filled from numeric ids, which is exactly what they
exist to avoid. DDL only; no ORM tables are imported.
"""

import uuid

from sqlalchemy import text

from backend.app.migrations.helpers import add_column, column_exists, table_exists

version = 217
name = "job_lineage"


async def _give_each_auto_job_an_identity(conn) -> None:
    await add_column(conn, "auto_queue_items", "recovery_identity VARCHAR(36)")
    rows = (
        await conn.execute(
            text("SELECT id FROM auto_queue_items WHERE recovery_identity IS NULL OR recovery_identity = ''")
        )
    ).all()
    for (row_id,) in rows:
        await conn.execute(
            text("UPDATE auto_queue_items SET recovery_identity = :identity WHERE id = :id"),
            {"identity": str(uuid.uuid4()), "id": row_id},
        )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_auto_queue_items_recovery_identity "
            "ON auto_queue_items (recovery_identity)"
        )
    )


async def _lineage_column(conn, table: str, column: str) -> None:
    await add_column(conn, table, f"{column} VARCHAR(36)")
    if await column_exists(conn, table, column):
        await conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})"))


async def upgrade(conn):
    if await table_exists(conn, "auto_queue_items"):
        await _give_each_auto_job_an_identity(conn)
    if await table_exists(conn, "print_queue"):
        await _lineage_column(conn, "print_queue", "source_auto_identity")
    if await table_exists(conn, "print_archives"):
        await _lineage_column(conn, "print_archives", "queue_item_identity")
        await _lineage_column(conn, "print_archives", "source_auto_identity")
