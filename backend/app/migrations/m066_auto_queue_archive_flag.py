"""Add ``print_archives.from_auto_queue`` + repair auto-queue lifecycle leaks.

The flag marks archives whose print was dispatched by the
AutoQueueScheduler (``auto_queue_items`` → ``print_queue``). The
``auto_queue_items`` row is a pre-dispatch *router*; under the new
lifecycle it is deleted together with its dispatched ``print_queue``
item, so the archive flag is the lasting record. It lets the auto-queue
view compute archive-backed completed / failed totals the same way
per-printer queues do (``get_queue_terminal_counts``).

Also repairs two pre-existing data issues. SQLite runs with
``foreign_keys=OFF`` so the ``ON DELETE`` clauses on these FKs never
fired, and the deletion code historically relied on them:

  * Backfill ``from_auto_queue=1`` for archives still traceable via a
    surviving ``print_queue.source_auto_item_id``. Historical completed
    auto-prints whose ``print_queue`` row was already auto-cleaned can't
    be recovered — that link is gone — they stay ``0``.
  * Delete ``auto_queue_items`` rows for finished auto-prints. An auto
    row is a pre-dispatch router — once its dispatched print is no
    longer in flight it is dead weight. This covers both the dangling
    orphans (``assigned_to_item_id`` points at a ``print_queue`` row
    that no longer exists — the completed-print auto-cleanup deleted it
    while ``foreign_keys=OFF``) AND rows whose ``print_queue`` item
    still exists but has reached a terminal status
    (``completed`` / ``failed`` / ``cancelled`` / ``skipped``). Only
    rows whose dispatched item is still ``pending`` / ``printing`` —
    or auto rows not yet dispatched at all — survive.

``calibration_session`` orphans and the ``spool_k_profile`` orphan are
intentionally left for manual cleanup — out of scope here.

Idempotent: the column is guarded by ``add_column``; the backfill and
orphan delete are naturally idempotent (re-running sets the same rows /
finds no orphans). Safe under ``DEBUG=true`` latest-migration re-runs.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column, table_exists

version = 66
name = "auto_queue_archive_flag"


async def upgrade(conn) -> None:
    if not await table_exists(conn, "print_archives"):
        return

    await add_column(conn, "print_archives", "from_auto_queue BOOLEAN NOT NULL DEFAULT 0")

    # Backfill: archives still reachable via a surviving print_queue row
    # that carries source_auto_item_id (in-flight + failed/cancelled items
    # that weren't auto-cleaned).
    if await table_exists(conn, "print_queue"):
        await conn.execute(
            text(
                "UPDATE print_archives SET from_auto_queue = 1 "
                "WHERE id IN ("
                "  SELECT archive_id FROM print_queue "
                "  WHERE source_auto_item_id IS NOT NULL AND archive_id IS NOT NULL"
                ")"
            )
        )

        # Delete auto_queue_items for finished auto-prints — both the
        # dangling orphans (print_queue row gone) and rows whose
        # print_queue item still exists but has reached a terminal status.
        # Rows still pending / printing — or never dispatched — survive.
        if await table_exists(conn, "auto_queue_items"):
            await conn.execute(
                text(
                    "DELETE FROM auto_queue_items "
                    "WHERE assigned_to_item_id IS NOT NULL "
                    "AND ("
                    "  assigned_to_item_id NOT IN (SELECT id FROM print_queue)"
                    "  OR assigned_to_item_id IN ("
                    "    SELECT id FROM print_queue WHERE status NOT IN ('pending', 'printing')"
                    "  )"
                    ")"
                )
            )
