"""Create ``project_print_plan_items`` for per-file print-plan rows.

Each project can carry a flat, ordered list of its ``.3mf`` library files
annotated with a ``copies`` count. Totals (grams, minutes, objects, cost)
are derived at read time from ``library_files.file_metadata`` and never
cached here — reslicing a 3MF flows through automatically.

The seed backfills one plan row per existing ``library_files.project_id``
so projects that were already linked before the feature landed don't look
empty. Backfill only touches ``.3mf`` files (type filter matches the
live auto-sync logic in ``api/routes/library.py``). Fresh installs get an
empty table.
"""

from sqlalchemy import text

version = 16
name = "project_print_plan"


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS project_print_plan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    library_file_id INTEGER NOT NULL REFERENCES library_files(id) ON DELETE CASCADE,
    copies INTEGER NOT NULL DEFAULT 1,
    order_index INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_plan_library_file UNIQUE (library_file_id)
)
"""

_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_project_print_plan_items_project_id ON project_print_plan_items(project_id)"
)


async def upgrade(conn):
    await conn.execute(text(_CREATE_TABLE))
    await conn.execute(text(_CREATE_INDEX))


async def seed(session_factory):
    # Backfill plan rows for files already linked to a project via
    # library_files.project_id — only .3mf. Ordering: creation order
    # within each project (id ASC).
    async with session_factory() as db:
        await db.execute(
            text(
                """
                INSERT INTO project_print_plan_items
                    (project_id, library_file_id, copies, order_index)
                SELECT
                    lf.project_id,
                    lf.id,
                    1,
                    ROW_NUMBER() OVER (PARTITION BY lf.project_id ORDER BY lf.id) - 1
                FROM library_files lf
                WHERE lf.project_id IS NOT NULL
                  AND LOWER(lf.file_type) = '3mf'
                  AND NOT EXISTS (
                      SELECT 1 FROM project_print_plan_items p
                      WHERE p.library_file_id = lf.id
                  )
                """
            )
        )
        await db.commit()
