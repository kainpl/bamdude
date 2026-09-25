"""Scheduled AMS drying: ``drying_schedules`` (rules) + ``scheduled_dryings`` (runs).

Upstream Bambuddy #2703 has one-shot runs only; the recurring rule is ours
(owner's decision 2026-09-25, vault 60-specs/scheduled-drying-spec). Fresh
installs get both tables from ``create_all``; this creates them on existing
databases. The same DDL as the models: SERIAL / AUTOINCREMENT keys by dialect
(m176 is the pattern), and the indexes outside the table guard so a fresh
install — whose tables came from ``create_all`` — gets them all the same.
"""

import logging

from backend.app.migrations.helpers import table_exists

logger = logging.getLogger(__name__)

version = 184
name = "scheduled_drying"


async def upgrade(conn):
    sqlite = conn.dialect.name == "sqlite"
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    stamp = "DATETIME" if sqlite else "TIMESTAMP"
    false, true = ("0", "1") if sqlite else ("FALSE", "TRUE")

    if not await table_exists(conn, "drying_schedules"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE drying_schedules (
                id {pk},
                printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                ams_id INTEGER NOT NULL,
                temp INTEGER NOT NULL,
                duration_hours INTEGER NOT NULL,
                filament VARCHAR(50) NOT NULL DEFAULT '',
                rotate_tray BOOLEAN NOT NULL DEFAULT {false},
                start_time VARCHAR(5) NOT NULL,
                weekdays INTEGER NOT NULL DEFAULT 127,
                latest_start VARCHAR(5),
                enabled BOOLEAN NOT NULL DEFAULT {true},
                created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at {stamp} DEFAULT CURRENT_TIMESTAMP,
                updated_at {stamp} DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    if not await table_exists(conn, "scheduled_dryings"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE scheduled_dryings (
                id {pk},
                printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                ams_id INTEGER NOT NULL,
                temp INTEGER NOT NULL,
                duration_hours INTEGER NOT NULL,
                filament VARCHAR(50) NOT NULL DEFAULT '',
                rotate_tray BOOLEAN NOT NULL DEFAULT {false},
                schedule_id INTEGER REFERENCES drying_schedules(id) ON DELETE SET NULL,
                start_after {stamp},
                latest_start {stamp},
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                reason VARCHAR(40),
                detail TEXT,
                created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at {stamp} DEFAULT CURRENT_TIMESTAMP,
                started_at {stamp},
                completed_at {stamp}
            )
            """
        )

    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_drying_schedules_printer ON drying_schedules (printer_id)"
    )
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_scheduled_dryings_status ON scheduled_dryings (status)")
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_scheduled_dryings_printer ON scheduled_dryings (printer_id)"
    )
