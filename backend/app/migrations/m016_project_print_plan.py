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

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import table_exists

version = 16
name = "project_print_plan"


# ⚠️ Frozen: this is what every released SQLite install got, byte for byte
# (pinned by ``tests/unit/test_migration_ddl_dialects.py``). Only the
# PostgreSQL sibling below is new.
_CREATE_TABLE_SQLITE = """
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

# The same table said in PostgreSQL: ``SERIAL`` for SQLite's implicit rowid
# alias, ``TIMESTAMP`` for a type PostgreSQL does not have under the name
# ``DATETIME``. Same columns in the same order, same defaults, same constraint
# NAME — m044 later drops ``uq_plan_library_file`` by that name to widen it.
_CREATE_TABLE_POSTGRES = """
CREATE TABLE IF NOT EXISTS project_print_plan_items (
    id SERIAL PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    library_file_id INTEGER NOT NULL REFERENCES library_files(id) ON DELETE CASCADE,
    copies INTEGER NOT NULL DEFAULT 1,
    order_index INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_plan_library_file UNIQUE (library_file_id)
)
"""

_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_project_print_plan_items_project_id ON project_print_plan_items(project_id)"
)


def _create_table_ddl() -> str:
    if is_postgres():
        return _CREATE_TABLE_POSTGRES
    return _CREATE_TABLE_SQLITE


async def upgrade(conn):
    # ⚠️ This branch used to be dead on PostgreSQL, and the DDL was written as
    # if it always would be. ``init_db()`` is ``create_all()`` + the chain, so
    # while ``project_print_plan_items`` was still a model the table existed
    # before m016 ran and the SQLite-only text (AUTOINCREMENT, DATETIME) never
    # reached the server. m158 retires the model — on a FRESH PostgreSQL install
    # nothing pre-creates the table any more and this statement is what builds
    # it, so it has to exist in both dialects. ``IF NOT EXISTS`` would not have
    # saved it either: PostgreSQL parses the statement before the check.
    if not await table_exists(conn, "project_print_plan_items"):
        await conn.execute(text(_create_table_ddl()))
    await conn.execute(text(_CREATE_INDEX))


async def seed(session_factory):
    # Backfill plan rows for files already linked to a project via
    # library_files.project_id — only .3mf. Ordering: creation order
    # within each project (id ASC).
    #
    # Post-m044: library_files.project_id is gone. On fresh installs this
    # seed has nothing to backfill anyway (empty table), and on upgrade
    # installs that already passed m044 the column is also gone. Guard
    # the SELECT so it doesn't crash with "no such column".
    async with session_factory() as db:
        if is_postgres():
            col_check = await db.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='library_files' "
                    "AND column_name='project_id'"
                )
            )
            project_id_exists = col_check.scalar() is not None
        else:
            cols = (await db.execute(text("PRAGMA table_info(library_files)"))).fetchall()
            project_id_exists = any(row[1] == "project_id" for row in cols)

        if not project_id_exists:
            return

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
