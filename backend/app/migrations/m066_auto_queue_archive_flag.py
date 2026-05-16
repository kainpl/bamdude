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

  * Backfill ``from_auto_queue=1`` for archives produced by an
    auto-queue print, found via a surviving ``print_queue`` row whose
    ``source_auto_item_id`` is set. Restricted to non-``pending`` rows:
    ``print_queue.archive_id`` points at the *produced* archive only
    from dispatch onward — on a still-``pending`` row it isn't that
    archive yet, so a pending row would flag the wrong one. Completed
    auto-prints are unreachable: their ``print_queue`` row — the only
    place the auto-queue link ever lived — is deleted on completion, so
    backfill is best-effort for history and the flag is authoritative
    only for prints dispatched under the new code.
  * Delete orphaned ``auto_queue_items`` whose ``assigned_to_item_id``
    points at a ``print_queue`` row that no longer exists (the
    completed-print auto-cleanup deleted it while ``foreign_keys=OFF``).
    Auto rows whose ``print_queue`` item is still in a printer's queue
    are kept — even terminal ones: when the operator deletes that queue
    item the live lifecycle (``detach_print_queue_refs``) removes the
    auto row with it, so the one-time cleanup stays consistent with the
    forward behaviour and never deletes a row the user can still see.

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

    # Backfill: archives produced by an auto-queue print, found via a
    # surviving print_queue row with source_auto_item_id. Non-pending
    # only — print_queue.archive_id points at the produced archive just
    # from dispatch onward; on a pending row it isn't that archive yet.
    # Completed auto-prints lost their print_queue row (deleted on
    # completion), so they're unreachable — backfill is best-effort.
    if await table_exists(conn, "print_queue"):
        await conn.execute(
            text(
                "UPDATE print_archives SET from_auto_queue = 1 "
                "WHERE id IN ("
                "  SELECT archive_id FROM print_queue "
                "  WHERE source_auto_item_id IS NOT NULL "
                "  AND archive_id IS NOT NULL "
                "  AND status <> 'pending'"
                ")"
            )
        )

        # Delete orphaned auto_queue_items — assigned_to_item_id dangling
        # because its print_queue row was deleted (completed-print
        # auto-cleanup) while foreign_keys=OFF. Auto rows whose
        # print_queue item still exists are kept: the operator deleting
        # that queue item triggers detach_print_queue_refs, which removes
        # the auto row in step with the live lifecycle.
        if await table_exists(conn, "auto_queue_items"):
            await conn.execute(
                text(
                    "DELETE FROM auto_queue_items "
                    "WHERE assigned_to_item_id IS NOT NULL "
                    "AND assigned_to_item_id NOT IN (SELECT id FROM print_queue)"
                )
            )
