"""Backfill filament_used_grams for failed / partial prints from actual usage.

Failed / aborted / cancelled / stopped prints stored the **full slicer estimate**
in ``print_archives.filament_used_grams`` (written at archive creation), so the
stats page over-counted filament for failures and disagreed with inventory. The
actual partial consumption was always recorded per spool in
``spool_usage_history`` (and deducted from inventory). This one-time backfill
sets each such archive's total to the sum of what was actually deducted, so
stats matches inventory retroactively.

Scope / safety:
- Only NON-completed terminal statuses (failed/aborted/cancelled/stopped).
  Completed prints already store the actual (estimate == actual at 100%).
- Only archives that actually have ``spool_usage_history`` rows (tracked prints).
  Untracked failures have no measured actual to substitute, so they keep the
  estimate and are left untouched.
- Correlated-subquery UPDATE — portable across SQLite and PostgreSQL.
- Idempotent: re-running sets the same sum.

Pairs with the live fix in ``usage_tracker.on_print_complete`` (which now writes
the actual weight for new partial prints).
"""

from sqlalchemy import text

version = 83
name = "backfill_failed_print_filament"


async def upgrade(conn):
    await conn.execute(
        text(
            """
            UPDATE print_archives
            SET filament_used_grams = (
                SELECT SUM(weight_used)
                FROM spool_usage_history
                WHERE spool_usage_history.archive_id = print_archives.id
            )
            WHERE status IN ('failed', 'aborted', 'cancelled', 'stopped')
              AND EXISTS (
                SELECT 1 FROM spool_usage_history
                WHERE spool_usage_history.archive_id = print_archives.id
              )
            """
        )
    )
