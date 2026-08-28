"""Filament family catalog, user tier (spec A §1). Two tables + one spool column.

The SYSTEM tier of the catalog ships as app data
(backend/app/data/filament_catalog/*.json) and deliberately never touches the
DB — this migration only creates the user-space side: mirrors of private
cloud presets and custom families. The backfill of spool.filament_family_id
is NOT here: resolution needs the catalog AND the mirrors, and a migration
must not touch the network — services/filament_family_backfill.py runs it at
startup after the first sync.
"""

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import add_column, table_exists

version = 149
name = "filament_families"


async def upgrade(conn):
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME" if sqlite else "TIMESTAMP"

    if not await table_exists(conn, "user_filament_presets"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE user_filament_presets (
                id {pk},
                owner_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                ecosystem VARCHAR(10) NOT NULL,
                source VARCHAR(12) NOT NULL,
                cloud_id VARCHAR(64),
                local_preset_id INTEGER REFERENCES local_presets(id) ON DELETE CASCADE,
                name VARCHAR(300) NOT NULL,
                family_filament_id VARCHAR(50),
                base_ref VARCHAR(300),
                vendor VARCHAR(200),
                filament_type VARCHAR(50),
                nozzle_temp_min INTEGER,
                nozzle_temp_max INTEGER,
                updated_time VARCHAR(40),
                synced_at {ts} DEFAULT CURRENT_TIMESTAMP NOT NULL,
                CONSTRAINT uq_user_fila_preset_cloud UNIQUE (owner_user_id, ecosystem, cloud_id),
                CONSTRAINT uq_user_fila_preset_local UNIQUE (local_preset_id)
            )
            """
        )

    if not await table_exists(conn, "user_filament_families"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE user_filament_families (
                id {pk},
                filament_id VARCHAR(50) NOT NULL,
                ecosystem VARCHAR(10) NOT NULL,
                alias VARCHAR(200) NOT NULL,
                vendor VARCHAR(200),
                filament_type VARCHAR(50),
                origin VARCHAR(12) NOT NULL,
                orphaned BOOLEAN NOT NULL DEFAULT {"0" if sqlite else "FALSE"},
                created_at {ts} DEFAULT CURRENT_TIMESTAMP NOT NULL,
                CONSTRAINT uq_user_fila_family UNIQUE (ecosystem, filament_id)
            )
            """
        )

    await add_column(conn, "spool", "filament_family_id VARCHAR(50)")
