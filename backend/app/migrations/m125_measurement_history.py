"""Keep what the plugs and sensors measured.

Two tables, both write-heavy and both pruned by age. Nothing derives a total
from either — the lifetime energy counter stays in
``smart_plug_energy_snapshots`` and on the print archive — so a gap here costs a
chart and nothing else.

The keys are BIGINT. Retention bounds how many rows a table holds, never the
counter, and PostgreSQL's usual SERIAL is 32-bit: at this write rate a large
farm would reach that ceiling inside a decade and stop, mid-insert, years from
now. On SQLite it changes nothing — INTEGER PRIMARY KEY is the 64-bit rowid.

Both indexes exist because every read is "this device, this window". Without
them that is a full scan of millions of rows: fast on the day it ships, slow a
month later, and slower every day after.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import table_exists

logger = logging.getLogger(__name__)

version = 125
name = "measurement_history"


async def upgrade(conn):
    sqlite = is_sqlite()
    # SQLite has no BIGSERIAL, and its INTEGER PRIMARY KEY is already 64-bit.
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "BIGSERIAL PRIMARY KEY"
    ts = "DATETIME" if sqlite else "TIMESTAMP"
    real = "REAL" if sqlite else "DOUBLE PRECISION"

    if not await table_exists(conn, "smart_plug_power_history"):
        await conn.execute(
            text(
                f"""
                CREATE TABLE smart_plug_power_history (
                    id {pk},
                    plug_id INTEGER NOT NULL REFERENCES smart_plugs(id) ON DELETE CASCADE,
                    power {real} NOT NULL,
                    recorded_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        logger.info("m125: created smart_plug_power_history")

    if not await table_exists(conn, "smart_sensor_history"):
        await conn.execute(
            text(
                f"""
                CREATE TABLE smart_sensor_history (
                    id {pk},
                    sensor_id INTEGER NOT NULL REFERENCES smart_sensors(id) ON DELETE CASCADE,
                    sensor_kind VARCHAR(32) NOT NULL,
                    value {real} NOT NULL,
                    recorded_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        logger.info("m125: created smart_sensor_history")

    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_plug_power_history_plug_time "
            "ON smart_plug_power_history (plug_id, recorded_at)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_sensor_history_sensor_kind_time "
            "ON smart_sensor_history (sensor_id, sensor_kind, recorded_at)"
        )
    )
