"""Locations become a tree.

Two engines, two different operations, because what has to go is a table
CONSTRAINT and not an index:

* SQLite has no DROP CONSTRAINT at any version -- ALTER TABLE DROP COLUMN,
  which it has had since 3.35 and which this repository uses elsewhere, is a
  different thing -- so the table is rebuilt.
* PostgreSQL drops the constraints by name, and the names are whatever the
  engine assigned. They are looked up rather than guessed: a DROP CONSTRAINT
  against a guessed name fails the upgrade on any install whose table was
  created by a different path.

Uniqueness becomes (parent_id, name_key). The index is a backstop only -- on
SQLite NULL != NULL, so two roots sharing a name pass it, and the route's own
check is what actually refuses them.
"""

import logging

from sqlalchemy import text

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import add_column, column_exists, recreate_table

logger = logging.getLogger(__name__)

version = 126
name = "location_hierarchy"

# SQLite only -- the PostgreSQL branch never rebuilds the table, so this
# deliberately does not branch on dialect the way m124's CREATE did.
_NEW_DDL = """
CREATE TABLE printer_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) NOT NULL,
    name_key VARCHAR(100) NOT NULL,
    parent_id INTEGER REFERENCES printer_locations(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""


async def upgrade(conn):
    if await column_exists(conn, "printer_locations", "parent_id"):
        logger.info("m126: printer_locations already has parent_id")
    elif is_sqlite():
        # The rebuild is what removes UNIQUE(name) and UNIQUE(name_key); it also
        # takes the old indexes with the old table.
        await recreate_table(conn, "printer_locations", _NEW_DDL, "id, name, name_key, created_at, updated_at")
        logger.info("m126: rebuilt printer_locations without the global unique constraints")
    else:
        await add_column(conn, "printer_locations", "parent_id INTEGER REFERENCES printer_locations(id)")
        found = await conn.execute(
            text("SELECT conname FROM pg_constraint WHERE conrelid = 'printer_locations'::regclass AND contype = 'u'")
        )
        for (constraint,) in found.all():
            await conn.execute(text(f'ALTER TABLE printer_locations DROP CONSTRAINT IF EXISTS "{constraint}"'))
            logger.info("m126: dropped unique constraint %s", constraint)

    # A no-op on SQLite, where the rebuild already took it.
    await conn.execute(text("DROP INDEX IF EXISTS ix_printer_locations_name_key"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_printer_locations_name_key ON printer_locations (name_key)"))
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_printer_locations_parent_name "
            "ON printer_locations (parent_id, name_key)"
        )
    )
