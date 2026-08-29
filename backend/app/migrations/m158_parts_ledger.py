"""m158: parts ledger — per-part plate state + project part targets.

Two tables. ``print_archive_parts`` is the live per-part state of one
printed plate (seeded at print start; skips and the defect dialog write
into it). ``project_parts`` is the project-wide target ledger keyed by
canonical part name. Design: docs/superpowers/specs/
2026-08-29-project-parts-ledger-design.md.

Fresh installs get both from ``create_all()``; every statement is guarded.
FK CASCADE is honoured by PostgreSQL only — this codebase never sets
``PRAGMA foreign_keys`` on SQLite; hard-delete paths clean up explicitly.
"""

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import table_exists

version = 158
name = "parts_ledger"


async def upgrade(conn):
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    json_t = "TEXT" if sqlite else "JSON"

    if not await table_exists(conn, "print_archive_parts"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE print_archive_parts (
                id {pk},
                archive_id INTEGER NOT NULL REFERENCES print_archives(id) ON DELETE CASCADE,
                name VARCHAR(512) NOT NULL,
                name_key VARCHAR(512) NOT NULL,
                identify_ids {json_t},
                quantity INTEGER NOT NULL DEFAULT 1,
                defective INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await conn.exec_driver_sql("CREATE INDEX ix_print_archive_parts_archive_id ON print_archive_parts (archive_id)")
        await conn.exec_driver_sql("CREATE INDEX ix_print_archive_parts_name_key ON print_archive_parts (name_key)")

    if not await table_exists(conn, "project_parts"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE project_parts (
                id {pk},
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name VARCHAR(512) NOT NULL,
                name_key VARCHAR(512) NOT NULL,
                target_qty INTEGER NOT NULL DEFAULT 0,
                CONSTRAINT uq_project_parts_key UNIQUE (project_id, name_key)
            )
            """
        )
        await conn.exec_driver_sql("CREATE INDEX ix_project_parts_project_id ON project_parts (project_id)")
