"""Library files & folders → many-to-many with projects.

Replaces the single-FK ``library_files.project_id`` and
``library_folders.project_id`` columns with two pivot tables
(``library_file_projects``, ``library_folder_projects``). Also reshapes
``project_print_plan_items`` unique constraint from ``(library_file_id)``
to ``(project_id, library_file_id)`` so a file in N projects can have N
independent plan rows.

Idempotent. On fresh installs ``Base.metadata.create_all`` materialises
the new tables in the new shape, and m044 finds nothing to migrate. On
upgrade installs m044 backfills, NULLs the legacy columns (debug aid),
and recreates the tables without ``project_id`` + with the new plan
constraint. Orphan FK values (``library_files.project_id`` referencing a
project that no longer exists — possible on DBs from pre-cascade days)
are dropped with a WARNING log so silent data loss is visible.

PRAGMA foreign_keys is OFF on SQLite installs (BamDude doesn't enable
it globally — see m018/m041 notes), so the recreate path is safe even
while ``project_print_plan_items`` references ``library_files`` mid-
transaction.
"""

import logging

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import column_exists, recreate_table

logger = logging.getLogger(__name__)

version = 44
name = "library_project_pivots"


# DDL for the recreated tables. SQLite uses these verbatim through
# recreate_table; PostgreSQL ignores them and just drops the offending
# columns / constraints in place.

_LIBRARY_FILES_NEW_DDL = """
CREATE TABLE library_files (
    id INTEGER PRIMARY KEY,
    folder_id INTEGER REFERENCES library_folders(id) ON DELETE CASCADE,
    is_external BOOLEAN DEFAULT 0 NOT NULL,
    filename VARCHAR(255) NOT NULL,
    file_path VARCHAR(500) NOT NULL,
    file_type VARCHAR(10) NOT NULL,
    file_tags JSON DEFAULT '[]' NOT NULL,
    file_size INTEGER NOT NULL,
    file_hash VARCHAR(64),
    thumbnail_path VARCHAR(500),
    file_metadata JSON,
    print_count INTEGER NOT NULL DEFAULT 0,
    last_printed_at DATETIME,
    notes TEXT,
    source_type VARCHAR(32),
    source_url VARCHAR(512),
    created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    swap_compatible BOOLEAN DEFAULT 0 NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    deleted_at DATETIME
)
"""

_LIBRARY_FOLDERS_NEW_DDL = """
CREATE TABLE library_folders (
    id INTEGER PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    parent_id INTEGER REFERENCES library_folders(id) ON DELETE CASCADE,
    is_external BOOLEAN DEFAULT 0 NOT NULL,
    external_readonly BOOLEAN DEFAULT 0 NOT NULL,
    external_show_hidden BOOLEAN DEFAULT 0 NOT NULL,
    external_path VARCHAR(500),
    archive_id INTEGER REFERENCES print_archives(id) ON DELETE SET NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

_PLAN_NEW_DDL = """
CREATE TABLE project_print_plan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    library_file_id INTEGER NOT NULL REFERENCES library_files(id) ON DELETE CASCADE,
    copies INTEGER NOT NULL DEFAULT 1,
    order_index INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_plan_project_file UNIQUE (project_id, library_file_id)
)
"""

# Columns to preserve in each recreate — must list every surviving column
# in the order it appears in the new DDL. Missing a column would either
# fail INSERT (NOT NULL) or silently drop data.

_LF_KEEP_COLS = (
    "id, folder_id, is_external, filename, file_path, file_type, file_tags, "
    "file_size, file_hash, thumbnail_path, file_metadata, print_count, "
    "last_printed_at, notes, source_type, source_url, created_by_id, "
    "swap_compatible, created_at, updated_at, deleted_at"
)
_FOLDER_KEEP_COLS = (
    "id, name, parent_id, is_external, external_readonly, external_show_hidden, "
    "external_path, archive_id, created_at, updated_at"
)
_PLAN_KEEP_COLS = "id, project_id, library_file_id, copies, order_index, created_at, updated_at"


async def _has_constraint_or_index(conn, name: str) -> bool:
    """Return True if a UNIQUE constraint / SQLite index of this name exists."""
    if is_postgres():
        result = await conn.execute(
            text("SELECT conname FROM pg_constraint WHERE conname=:n"),
            {"n": name},
        )
    else:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE name=:n"),
            {"n": name},
        )
    return result.scalar() is not None


async def upgrade(conn):
    # 1. Pivot tables — `IF NOT EXISTS` covers fresh installs where
    # `Base.metadata.create_all` already materialised them from the
    # `library_project_links` module.
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS library_file_projects (
                file_id    INTEGER NOT NULL REFERENCES library_files(id)  ON DELETE CASCADE,
                project_id INTEGER NOT NULL REFERENCES projects(id)       ON DELETE CASCADE,
                PRIMARY KEY (file_id, project_id)
            )
            """
        )
    )
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_lfp_project_id ON library_file_projects (project_id)"))
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS library_folder_projects (
                folder_id  INTEGER NOT NULL REFERENCES library_folders(id) ON DELETE CASCADE,
                project_id INTEGER NOT NULL REFERENCES projects(id)        ON DELETE CASCADE,
                PRIMARY KEY (folder_id, project_id)
            )
            """
        )
    )
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_lfop_project_id ON library_folder_projects (project_id)"))

    # 2. Backfill from legacy columns (only if they still exist — fresh
    # installs already have the new shape and skip this block entirely).
    if await column_exists(conn, "library_files", "project_id"):
        orphan_files = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM library_files "
                    "WHERE project_id IS NOT NULL "
                    "AND project_id NOT IN (SELECT id FROM projects)"
                )
            )
        ).scalar_one()
        if orphan_files:
            logger.warning(
                "m044: dropping %d orphan library_files.project_id values (referenced project no longer exists)",
                orphan_files,
            )
        await conn.execute(
            text(
                """
                INSERT INTO library_file_projects (file_id, project_id)
                SELECT id, project_id FROM library_files
                WHERE project_id IS NOT NULL
                  AND project_id IN (SELECT id FROM projects)
                ON CONFLICT DO NOTHING
                """
            )
        )
        # Debug aid (plan §A.2.5): zero out before recreate so an inspector
        # mid-migration sees an unambiguous "data already in pivot" state.
        await conn.execute(text("UPDATE library_files SET project_id = NULL WHERE project_id IS NOT NULL"))
        await recreate_table(conn, "library_files", _LIBRARY_FILES_NEW_DDL, _LF_KEEP_COLS)
        # Re-issue the indexes that lived on the original table
        # (m029 deleted_at, m033 source_url) — they're tied to the
        # table identity, so the SQLite recreate path drops them.
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_library_files_deleted_at ON library_files(deleted_at)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_library_files_source_url ON library_files(source_url)"))

    if await column_exists(conn, "library_folders", "project_id"):
        orphan_folders = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM library_folders "
                    "WHERE project_id IS NOT NULL "
                    "AND project_id NOT IN (SELECT id FROM projects)"
                )
            )
        ).scalar_one()
        if orphan_folders:
            logger.warning(
                "m044: dropping %d orphan library_folders.project_id values (referenced project no longer exists)",
                orphan_folders,
            )
        await conn.execute(
            text(
                """
                INSERT INTO library_folder_projects (folder_id, project_id)
                SELECT id, project_id FROM library_folders
                WHERE project_id IS NOT NULL
                  AND project_id IN (SELECT id FROM projects)
                ON CONFLICT DO NOTHING
                """
            )
        )
        await conn.execute(text("UPDATE library_folders SET project_id = NULL WHERE project_id IS NOT NULL"))
        await recreate_table(conn, "library_folders", _LIBRARY_FOLDERS_NEW_DDL, _FOLDER_KEEP_COLS)

    # Audit log — runs on every install, including fresh, so operators
    # can verify the post-migration link count from boot logs.
    file_links = (await conn.execute(text("SELECT COUNT(*) FROM library_file_projects"))).scalar_one()
    folder_links = (await conn.execute(text("SELECT COUNT(*) FROM library_folder_projects"))).scalar_one()
    logger.info(
        "m044: %d file→project links, %d folder→project links present after backfill",
        file_links,
        folder_links,
    )

    # 3. Print-plan unique constraint reshape: (library_file_id) →
    # (project_id, library_file_id). Skip when the new constraint is
    # already in place (fresh install or re-run).
    if not await _has_constraint_or_index(conn, "uq_plan_project_file"):
        if is_postgres():
            await conn.execute(
                text("ALTER TABLE project_print_plan_items DROP CONSTRAINT IF EXISTS uq_plan_library_file")
            )
            await conn.execute(
                text(
                    "ALTER TABLE project_print_plan_items "
                    "ADD CONSTRAINT uq_plan_project_file "
                    "UNIQUE (project_id, library_file_id)"
                )
            )
        else:
            await recreate_table(conn, "project_print_plan_items", _PLAN_NEW_DDL, _PLAN_KEEP_COLS)
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_project_print_plan_items_project_id "
                    "ON project_print_plan_items(project_id)"
                )
            )
