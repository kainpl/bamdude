"""Cameras that belong to no printer: a room, a shelf, a dryer.

Vault 60-specs/standalone-cameras-spec. A printer's external camera is a set of
columns on ``printers`` and REPLACES that printer's own camera — every consumer
of its frames (finish photo, plate check, Obico, layer timelapse) reads through
it. This table is the other thing entirely: a camera that shows a place. It is
only ever displayed — the wall, a button on its location's group header, a
floating window — and never feeds a printer's consumers.

``location_id`` reuses ``printer_locations``, the table a printer and an adopted
sensor already point at, with ``RESTRICT`` like the sensor's: a location still
holding a camera refuses to be deleted. ⚠️ SQLite never gets ``PRAGMA
foreign_keys = ON`` in this codebase, so that refusal is enforced by the
location route's own guard; the constraint is the PostgreSQL backstop.

``name`` is unique because it is what the wall tile, the location button and
the window title all say — two cameras called "Shelf" would be a coin toss.
"""

import logging

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import table_exists

logger = logging.getLogger(__name__)

version = 179
name = "cameras"


async def upgrade(conn):
    if await table_exists(conn, "cameras"):
        return

    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    stamp = "DATETIME" if sqlite else "TIMESTAMP"
    true_default = "1" if sqlite else "true"

    await conn.exec_driver_sql(
        f"""
        CREATE TABLE cameras (
            id {pk},
            name VARCHAR(100) NOT NULL UNIQUE,
            camera_type VARCHAR(20) NOT NULL,
            url VARCHAR(500) NOT NULL,
            snapshot_url VARCHAR(500),
            rotation INTEGER NOT NULL DEFAULT 0,
            enabled BOOLEAN NOT NULL DEFAULT {true_default},
            location_id INTEGER REFERENCES printer_locations(id) ON DELETE RESTRICT,
            created_at {stamp} NOT NULL,
            updated_at {stamp} NOT NULL
        )
        """
    )
    await conn.exec_driver_sql("CREATE INDEX ix_cameras_location_id ON cameras (location_id)")
    logger.info("m179: cameras table created")
