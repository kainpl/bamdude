"""Archive columns for queue/batch/error_message + drop stale queue counters.

Shifts queue history into ``print_archives`` so the live queue table stays
lean and auto-cleans completed items. Three new archive columns:

* ``queue_id`` — FK to ``printer_queues(id)``, indexed, ``SET NULL`` on
  delete. For rows that came through the queue (either library-file or
  reprint path) it matches the originating queue item's ``queue_id``;
  for external / direct-dispatch prints it gets the printer's default
  queue so stats queries don't miss these rows.
* ``batch_id`` — VARCHAR(36), indexed. Carried over from the queue item
  so we can still answer "how many of batch <uuid> completed?" after
  the queue row is cleaned up.
* ``error_message`` — full diagnostic text (free-form). Short cause codes
  continue to live in the existing ``failure_reason`` VARCHAR(100); this
  is the verbose twin operators see on hover.

Seeds each archive that has a linked queue item (``print_queue.archive_id``
points at it) with the item's ``queue_id``/``batch_id``/``error_message``.
Archives without a queue link fall back to the printer's default queue id
so on-the-fly counters (``GET /printer-queues/`` completed/failed/cancelled)
see them.

Finally, deletes completed queue items that have an ``archive_id`` link —
the backfill equivalent of the new ``on_print_complete`` auto-cleanup, so
pre-migration history doesn't sit around in the live queue table (failed /
cancelled / skipped rows are kept for operator follow-up).

Also drops the cached ``completed_count`` / ``failed_count`` /
``cancelled_count`` / ``total_count`` columns from ``printer_queues`` —
they become stale the moment we start auto-cleaning queue items, and the
replacement counter source is ``print_archives`` via the new ``queue_id``.
``pending_count`` and ``skipped_count`` stay (they track live-state queue
items which aren't cleaned up).

Idempotent: ``add_column`` skips existing columns; ``recreate_table`` on
``printer_queues`` runs unconditionally but preserves live data through
the copy phase, with a row-count assertion just like m018.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column, recreate_table

version = 19
name = "archive_queue_batch_error"


_PRINTER_QUEUES_NEW_DDL = """CREATE TABLE printer_queues (
    id INTEGER PRIMARY KEY,
    printer_id INTEGER NOT NULL UNIQUE REFERENCES printers(id) ON DELETE CASCADE,
    status VARCHAR(20) NOT NULL DEFAULT 'idle',
    last_activity_at DATETIME,
    current_item_id INTEGER,
    pending_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)"""

_PRINTER_QUEUES_KEEP_COLUMNS = (
    "id, printer_id, status, last_activity_at, current_item_id, pending_count, skipped_count, created_at, updated_at"
)


async def upgrade(conn):
    # 1) Add the three archive columns (idempotent).
    await add_column(
        conn,
        "print_archives",
        "queue_id INTEGER REFERENCES printer_queues(id) ON DELETE SET NULL",
    )
    await add_column(conn, "print_archives", "batch_id VARCHAR(36)")
    await add_column(conn, "print_archives", "error_message TEXT")

    # Helpful indexes for the replacement stats queries.
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_print_archives_queue_id ON print_archives(queue_id)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_print_archives_batch_id ON print_archives(batch_id)"))

    # 2) Drop stale cached counters from printer_queues.
    if is_postgres():
        for col in ("completed_count", "failed_count", "cancelled_count", "total_count"):
            await conn.execute(text(f"ALTER TABLE printer_queues DROP COLUMN IF EXISTS {col}"))
    else:
        before = (await conn.execute(text("SELECT COUNT(*) FROM printer_queues"))).scalar() or 0
        await recreate_table(conn, "printer_queues", _PRINTER_QUEUES_NEW_DDL, _PRINTER_QUEUES_KEEP_COLUMNS)
        after = (await conn.execute(text("SELECT COUNT(*) FROM printer_queues"))).scalar() or 0
        if after != before:
            raise RuntimeError(f"m019 printer_queues recreate lost rows: before={before} after={after}")


async def seed(session_factory):
    """Backfill archive.queue_id / batch_id / error_message from live data."""
    async with session_factory() as db:
        # Pass 1 — archives linked to a queue item copy its fields.
        await db.execute(
            text(
                """
                UPDATE print_archives
                SET queue_id = (
                    SELECT pq.queue_id FROM print_queue pq
                    WHERE pq.archive_id = print_archives.id
                    LIMIT 1
                )
                WHERE queue_id IS NULL
                  AND EXISTS (SELECT 1 FROM print_queue pq WHERE pq.archive_id = print_archives.id)
                """
            )
        )
        await db.execute(
            text(
                """
                UPDATE print_archives
                SET batch_id = (
                    SELECT pq.batch_id FROM print_queue pq
                    WHERE pq.archive_id = print_archives.id
                    LIMIT 1
                )
                WHERE batch_id IS NULL
                  AND EXISTS (
                      SELECT 1 FROM print_queue pq
                      WHERE pq.archive_id = print_archives.id AND pq.batch_id IS NOT NULL
                  )
                """
            )
        )
        await db.execute(
            text(
                """
                UPDATE print_archives
                SET error_message = (
                    SELECT pq.error_message FROM print_queue pq
                    WHERE pq.archive_id = print_archives.id
                    LIMIT 1
                )
                WHERE error_message IS NULL
                  AND EXISTS (
                      SELECT 1 FROM print_queue pq
                      WHERE pq.archive_id = print_archives.id AND pq.error_message IS NOT NULL
                  )
                """
            )
        )

        # Pass 2 — archives that never went through the queue (external /
        # direct-dispatch) fall back to the printer's default queue id so
        # they're still counted in archive-driven stats.
        await db.execute(
            text(
                """
                UPDATE print_archives
                SET queue_id = (
                    SELECT pq.id FROM printer_queues pq
                    WHERE pq.printer_id = print_archives.printer_id
                    LIMIT 1
                )
                WHERE queue_id IS NULL
                  AND printer_id IS NOT NULL
                """
            )
        )

        # Pass 3 — one-shot historical cleanup. Going forward
        # ``on_print_complete`` auto-deletes completed queue items that have
        # an ``archive_id`` link (their metadata now lives on the archive
        # row via ``queue_id``/``batch_id``). Backfill the backlog by
        # applying the same rule to pre-migration history. Failed /
        # cancelled / skipped stay intact — operators may still want to
        # retry or unskip them.
        await db.execute(
            text(
                """
                DELETE FROM print_queue
                WHERE status = 'completed'
                  AND archive_id IS NOT NULL
                """
            )
        )
        await db.commit()
