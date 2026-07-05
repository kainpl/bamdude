"""Create ``printer_sensor_history`` — heater readings (nozzle / bed / chamber).

Parallel to ``ams_sensor_history`` (humidity/temp per AMS unit), this stores
per-(printer, sensor_kind) heater samples so the Printers page can chart nozzle,
second-nozzle (H2D dual), bed and chamber temperatures over time. A background
recorder in ``main.py`` samples ``state.temperatures`` once a minute.

Fresh installs get the table from ``create_all`` via
``models/printer_sensor_history.py``; existing DBs get it here. Explicit DDL
(rather than ``__table__.create``) keeps this migration frozen against future
model edits. Idempotent via ``table_exists``.
"""

import logging

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import table_exists

logger = logging.getLogger(__name__)

version = 92
name = "printer_sensor_history"

_DDL_PG = """CREATE TABLE printer_sensor_history (
    id SERIAL PRIMARY KEY,
    printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
    sensor_kind VARCHAR(32),
    value DOUBLE PRECISION,
    target DOUBLE PRECISION,
    recorded_at TIMESTAMP DEFAULT now()
)"""

_DDL_SQLITE = """CREATE TABLE printer_sensor_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
    sensor_kind VARCHAR(32),
    value FLOAT,
    target FLOAT,
    recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP
)"""


async def upgrade(conn) -> None:
    if await table_exists(conn, "printer_sensor_history"):
        return

    await conn.execute(text(_DDL_PG if is_postgres() else _DDL_SQLITE))
    # Composite index for the per-sensor time-range queries the history route runs.
    await conn.execute(
        text(
            "CREATE INDEX ix_printer_sensor_history_printer_kind_time "
            "ON printer_sensor_history (printer_id, sensor_kind, recorded_at)"
        )
    )
    # Matches the model's ``index=True`` on recorded_at (create_all parity).
    await conn.execute(
        text("CREATE INDEX ix_printer_sensor_history_recorded_at ON printer_sensor_history (recorded_at)")
    )
    logger.info("m092: created printer_sensor_history table")
