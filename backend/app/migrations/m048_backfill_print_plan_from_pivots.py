"""Backfill ``project_print_plan_items`` from existing M2M pivots.

Pre-fix, the upload / extract-zip / sliced-output / MakerWorld-import paths
created ``library_files`` rows directly in a folder but never copied the
folder's project links onto the file's M2M (post-m044
``library_file_projects`` pivot) and never planted matching plan rows. The
move / patch flows already did both, so files **moved into** a project
folder showed up correctly; files **uploaded into** the same folder went
missing from the print plan.

m044's seed only ran the *one-shot* legacy ``library_files.project_id``
backfill — by definition that column was already gone for any post-m044
upload, so no plan row was ever planted for those files.

This migration walks the live ``library_file_projects`` pivot and ensures
every ``(project_id, library_file_id)`` pair has a matching
``project_print_plan_items`` row. Plan-eligible only (``library_files
.file_type = '3mf'``). Idempotent — ``WHERE NOT EXISTS`` guard skips
already-planted rows. Order index uses the existing ``MAX(order_index)``
per project as a base so new rows append after operator-curated entries
rather than reshuffling them.

Trashed files (``deleted_at IS NOT NULL``) are skipped — they shouldn't
appear in the live print plan.
"""

from sqlalchemy import text

version = 48
name = "backfill_print_plan_from_pivots"


_BACKFILL_SQL = """
INSERT INTO project_print_plan_items
    (project_id, library_file_id, copies, order_index)
SELECT
    lfp.project_id,
    lfp.file_id,
    1 AS copies,
    -- Append after the project's existing max(order_index) so curated rows
    -- keep their position. Multiple new rows for the same project share
    -- the same starting offset and tie-break by library_file_id (stable).
    COALESCE(
        (SELECT MAX(order_index) FROM project_print_plan_items p
         WHERE p.project_id = lfp.project_id),
        -1
    ) + 1 AS order_index
FROM library_file_projects lfp
JOIN library_files lf ON lf.id = lfp.file_id
WHERE LOWER(lf.file_type) = '3mf'
  AND lf.deleted_at IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM project_print_plan_items p
      WHERE p.project_id = lfp.project_id
        AND p.library_file_id = lfp.file_id
  )
"""


async def upgrade(conn):
    # Skip if the pivot table doesn't exist yet (fresh installs that haven't
    # finished the m044 chain — extremely unlikely, but cheap to guard).
    from backend.app.core.db_dialect import is_postgres

    if is_postgres():
        check = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name='library_file_projects'"
            )
        )
        if check.scalar() is None:
            return
    else:
        check = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='library_file_projects'")
        )
        if check.scalar() is None:
            return

    await conn.execute(text(_BACKFILL_SQL))
