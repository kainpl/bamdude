"""m164: printer tags — the label list and its links to printers.

Design: docs/superpowers/specs/2026-09-05-stagger-groups-design.md (Decision 7).

Two tables, no seed, no backfill: a farm has no tags until an operator makes
one. DDL is dialect-neutral (m162 is the pattern): ``create_all`` builds both
tables on a fresh install before this runs and the guards then do nothing; the
CREATE below is the path an EXISTING database walks, and on PostgreSQL that
path must parse (``tests/unit/test_migration_ddl_dialects.py``).
"""

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import table_exists

version = 164
name = "printer_tags"


async def upgrade(conn):
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME DEFAULT CURRENT_TIMESTAMP" if sqlite else "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"

    if not await table_exists(conn, "printer_tags"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE printer_tags (
                id {pk},
                name VARCHAR(64) NOT NULL,
                name_key VARCHAR(64) NOT NULL,
                created_at {ts} NOT NULL,
                updated_at {ts} NOT NULL
            )
            """
        )
    await conn.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ix_printer_tags_name_key ON printer_tags (name_key)")

    if not await table_exists(conn, "printer_tag_links"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE printer_tag_links (
                printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES printer_tags(id) ON DELETE CASCADE,
                created_at {ts} NOT NULL,
                PRIMARY KEY (printer_id, tag_id)
            )
            """
        )
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_printer_tag_links_tag ON printer_tag_links (tag_id)")
