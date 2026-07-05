"""Structured storage-locations catalog (upstream Bambuddy #1505 / af5d24e2).

Flat catalog of physical shelves/drawers/dryboxes. Adds a ``locations`` table and
a nullable ``spool.location_id`` FK. The denormalized free-text
``spool.storage_location`` is retained (Spoolman round-trip + display); this
migration backfills catalog rows from distinct existing ``storage_location``
values and back-links spools. ``spool.purchase_location`` is unrelated and left
untouched.

Fresh installs get the table/columns/indexes from ``create_all``; the guarded DDL
below is a no-op there and only does real work on existing DBs. The data backfill
is idempotent (INSERT-OR-IGNORE / ON CONFLICT DO NOTHING on ``name_key``).
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import add_column, column_exists, table_exists

version = 93
name = "storage_locations_catalog"


async def upgrade(conn):
    # 1) locations table (dialect-explicit)
    if not await table_exists(conn, "locations"):
        if is_sqlite():
            await conn.execute(
                text(
                    """
                    CREATE TABLE locations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(255) NOT NULL UNIQUE,
                        name_key VARCHAR(255),
                        identifier VARCHAR(100),
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        else:
            await conn.execute(
                text(
                    """
                    CREATE TABLE locations (
                        id SERIAL PRIMARY KEY,
                        name VARCHAR(255) NOT NULL UNIQUE,
                        name_key VARCHAR(255),
                        identifier VARCHAR(100),
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )

    if not await column_exists(conn, "locations", "name_key"):
        await add_column(conn, "locations", "name_key VARCHAR(255)")

    await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_locations_name_key ON locations (name_key)"))

    # 2) spool.location_id FK + index
    await add_column(conn, "spool", "location_id INTEGER REFERENCES locations(id)")
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_spool_location_id ON spool (location_id)"))

    # 3) backfill name_key on any legacy locations rows FIRST
    await conn.execute(
        text("UPDATE locations SET name_key = LOWER(TRIM(name)) WHERE name_key IS NULL OR TRIM(name_key) = ''")
    )

    # 4) backfill catalog rows from distinct free-text storage_location values
    if is_sqlite():
        backfill_sql = """
            INSERT OR IGNORE INTO locations (name, name_key, created_at, updated_at)
            SELECT MIN(TRIM(storage_location)), LOWER(TRIM(storage_location)),
                   CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            FROM spool
            WHERE TRIM(COALESCE(storage_location, '')) != ''
            GROUP BY LOWER(TRIM(storage_location))
        """
    else:
        backfill_sql = """
            INSERT INTO locations (name, name_key, created_at, updated_at)
            SELECT MIN(TRIM(storage_location)), LOWER(TRIM(storage_location)),
                   CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            FROM spool
            WHERE TRIM(COALESCE(storage_location, '')) != ''
            GROUP BY LOWER(TRIM(storage_location))
            ON CONFLICT (name_key) DO NOTHING
        """
    await conn.execute(text(backfill_sql))

    # 5) back-link spools to their catalog row (correlated subquery; SQLite + PG safe)
    await conn.execute(
        text(
            """
            UPDATE spool
            SET location_id = (
                SELECT l.id FROM locations l WHERE l.name_key = LOWER(TRIM(spool.storage_location)) LIMIT 1
            )
            WHERE TRIM(COALESCE(storage_location, '')) != '' AND location_id IS NULL
            """
        )
    )

    # 6) sanity log
    orphan = (
        await conn.execute(
            text("SELECT COUNT(*) FROM spool WHERE TRIM(COALESCE(storage_location, '')) != '' AND location_id IS NULL")
        )
    ).scalar() or 0
    if orphan:
        import logging

        logging.getLogger(__name__).warning(
            "m093: %d spool(s) kept free-text storage_location without a location_id link; re-save to link.", orphan
        )
