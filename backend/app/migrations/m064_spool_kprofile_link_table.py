"""Convert ``spool_k_profile`` + ``spoolman_k_profile`` into thin link tables.

Background: K-profile data (``k_value``, ``name``, ``cali_idx``, ``setting_id``,
``nozzle_type``) was previously duplicated on every spool that referenced the
same printer-side calibration. 100 generic-PETG spools all carried the same
0.025 K row. After m062/m063 ``filament_calibration`` exists as the per-printer
cache; spool→K becomes a single FK.

What this migration does (per table):
  1. DELETE all existing rows — the OLD data was per-spool snapshots of K
     values that drifted from reality, and ``filament_calibration`` is now
     repopulated from the printer's live ``extrusion_cali_get`` push on
     every MQTT (re)connect. The user re-links spools through the PA tab
     after the upgrade; that path runs find-or-create against fresh data
     instead of guessing from stale ``setting_id`` fields.
  2. Drop OLD K-data columns and add ``filament_calibration_id`` FK. SQLite
     can't ``DROP COLUMN`` columns referenced by inline UNIQUE / FK
     constraints (``spoolman_k_profile`` has ``UNIQUE(... nozzle_diameter)``)
     so rebuild via ``recreate_table``. Postgres uses explicit constraint
     drop + DROP COLUMN.

Idempotent: guarded by ``column_exists``.
"""

import logging

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import column_exists, recreate_table, table_exists

logger = logging.getLogger(__name__)

version = 64
name = "spool_kprofile_link_table"


# Target schemas for the SQLite recreate path. Mirror the post-m064 model
# definitions exactly so fresh installs (which create the table from the
# model) and upgraded installs (which run through this migration) converge.
_NEW_DDL_SPOOL_K_PROFILE = """CREATE TABLE spool_k_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spool_id INTEGER NOT NULL REFERENCES spool(id) ON DELETE CASCADE,
    printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
    extruder INTEGER NOT NULL DEFAULT 0,
    filament_calibration_id INTEGER NOT NULL REFERENCES filament_calibration(id) ON DELETE CASCADE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)"""

_NEW_DDL_SPOOLMAN_K_PROFILE = """CREATE TABLE spoolman_k_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spoolman_spool_id INTEGER NOT NULL,
    printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
    extruder INTEGER NOT NULL DEFAULT 0,
    filament_calibration_id INTEGER NOT NULL REFERENCES filament_calibration(id) ON DELETE CASCADE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_spoolman_kp UNIQUE (spoolman_spool_id, printer_id, extruder, filament_calibration_id),
    CONSTRAINT ck_spoolman_kp_extruder_range CHECK (extruder >= 0 AND extruder <= 1)
)"""

_COLUMNS_TO_COPY = {
    "spool_k_profile": "id, spool_id, printer_id, extruder, filament_calibration_id, created_at",
    "spoolman_k_profile": "id, spoolman_spool_id, printer_id, extruder, filament_calibration_id, created_at",
}

_NEW_DDLS = {
    "spool_k_profile": _NEW_DDL_SPOOL_K_PROFILE,
    "spoolman_k_profile": _NEW_DDL_SPOOLMAN_K_PROFILE,
}


async def _convert_table(conn, table: str) -> None:
    if not await table_exists(conn, table):
        return
    # Already migrated?
    if await column_exists(conn, table, "filament_calibration_id") and not await column_exists(conn, table, "k_value"):
        return

    # 1. Add the new column nullable so the row wipe below has somewhere to
    # land. The recreate / column-shape change at step 3 promotes it to
    # ``NOT NULL`` via the new DDL (SQLite) or the new constraint (PG).
    if not await column_exists(conn, table, "filament_calibration_id"):
        if is_postgres():
            await conn.execute(
                text(
                    f"ALTER TABLE {table} ADD COLUMN filament_calibration_id INTEGER "
                    "REFERENCES filament_calibration(id) ON DELETE CASCADE"
                )
            )
        else:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN filament_calibration_id INTEGER"))

    # 2. Drop ALL existing rows. The old K-data was per-spool snapshots that
    # drifted from reality; ``filament_calibration`` will be repopulated from
    # the printer's live push on MQTT (re)connect, and the user re-links
    # spools via the PA tab against that fresh state.
    dropped = (await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar() or 0
    if dropped:
        logger.warning(
            "m064: clearing %d %s rows (old per-spool K-data, will be re-attached via PA tab).", dropped, table
        )
        await conn.execute(text(f"DELETE FROM {table}"))

    # 3. Drop OLD K-data columns. SQLite's ``DROP COLUMN`` (3.35+) refuses
    # when an inline UNIQUE/CHECK constraint still references the column
    # (``spoolman_k_profile`` had ``UNIQUE(..., nozzle_diameter)``). Use the
    # established ``recreate_table`` dance: build the target table, copy rows
    # by name, drop the old, rename. Postgres needs to drop the named
    # constraint first since ``DROP COLUMN`` there errors without CASCADE.
    if is_postgres():
        if table == "spoolman_k_profile":
            await conn.execute(text("ALTER TABLE spoolman_k_profile DROP CONSTRAINT IF EXISTS uq_spoolman_kp"))
        for col in ("k_value", "name", "cali_idx", "setting_id", "nozzle_type", "nozzle_diameter"):
            if await column_exists(conn, table, col):
                await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {col}"))
        if table == "spoolman_k_profile":
            await conn.execute(
                text(
                    "ALTER TABLE spoolman_k_profile ADD CONSTRAINT uq_spoolman_kp "
                    "UNIQUE (spoolman_spool_id, printer_id, extruder, filament_calibration_id)"
                )
            )
    else:
        await recreate_table(conn, table, _NEW_DDLS[table], _COLUMNS_TO_COPY[table])
    # Post-state check: the table is empty by design after step 2, but the
    # recreate path could still lose schema if the new DDL drifts from the
    # columns_to_copy list.
    final_count = (await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar() or 0
    if final_count != 0:
        raise RuntimeError(f"m064: {table} non-empty after rewrite ({final_count} rows) — copy list bug?")


async def upgrade(conn) -> None:
    await _convert_table(conn, "spool_k_profile")
    await _convert_table(conn, "spoolman_k_profile")
