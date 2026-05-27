"""NULL orphaned spool_usage_history.archive_id references.

Before this release, deleting a print archive did **not** detach the
``spool_usage_history`` rows that referenced it: the ``archive_id`` FK has no
``ON DELETE`` clause, and the delete path never NULLed it. On SQLite (FK
enforcement off) that left rows pointing at a now-deleted archive id; on
PostgreSQL it would have blocked the delete outright. ``delete_archive`` now
NULLs ``archive_id`` up-front, but existing databases may still carry dangling
references from past deletes. This one-time backfill clears them.

Scope / safety:
- Only rows whose ``archive_id`` no longer matches any ``print_archives`` row.
  Soft-deleted archives still exist (``deleted_at`` stamped), so their usage
  rows are left attached.
- The usage rows themselves are kept — they remain the spool's consumption
  record; only the stale link is severed.
- ``NOT EXISTS`` correlated subquery — portable across SQLite and PostgreSQL.
- Idempotent: re-running matches nothing once cleared.
"""

from sqlalchemy import text

version = 84
name = "null_orphaned_usage_history_archive_id"


async def upgrade(conn):
    await conn.execute(
        text(
            """
            UPDATE spool_usage_history
            SET archive_id = NULL
            WHERE archive_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM print_archives
                WHERE print_archives.id = spool_usage_history.archive_id
              )
            """
        )
    )
