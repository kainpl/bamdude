"""User-authored library tags — catalog table + file M2M (#1268 / G7-G1).

Adds library_tags (global label catalog) and library_file_tags (M2M join,
composite PK, ON DELETE CASCADE both sides). DISTINCT from the m036
library_files.file_tags COMPUTED-badge column — this is user-authored labelling.
Fresh installs get both tables from create_all; guarded DDL below only does real
work on existing DBs. No backfill. Idempotent + DEBUG-re-run-safe.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import table_exists

version = 95
name = "library_user_tags"


async def upgrade(conn):
    if not await table_exists(conn, "library_tags"):
        if is_sqlite():
            await conn.execute(
                text("""
                CREATE TABLE library_tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(64) NOT NULL,
                    name_key VARCHAR(64) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            )
        else:
            await conn.execute(
                text("""
                CREATE TABLE library_tags (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(64) NOT NULL,
                    name_key VARCHAR(64) NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            )
    await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_library_tags_name_key ON library_tags (name_key)"))
    if not await table_exists(conn, "library_file_tags"):
        if is_sqlite():
            await conn.execute(
                text("""
                CREATE TABLE library_file_tags (
                    file_id INTEGER NOT NULL REFERENCES library_files(id) ON DELETE CASCADE,
                    tag_id INTEGER NOT NULL REFERENCES library_tags(id) ON DELETE CASCADE,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (file_id, tag_id)
                )
            """)
            )
        else:
            await conn.execute(
                text("""
                CREATE TABLE library_file_tags (
                    file_id INTEGER NOT NULL REFERENCES library_files(id) ON DELETE CASCADE,
                    tag_id INTEGER NOT NULL REFERENCES library_tags(id) ON DELETE CASCADE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (file_id, tag_id)
                )
            """)
            )
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_library_file_tags_tag_id ON library_file_tags (tag_id)"))
