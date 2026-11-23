"""Retain HMS notification state with each printer record."""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column

version = 256
name = "hms_notified_faults"


async def upgrade(conn):
    if conn.dialect.name == "postgresql":
        await conn.execute(text("ALTER TABLE printers ADD COLUMN IF NOT EXISTS hms_notified_faults JSON"))
    else:
        await add_column(conn, "printers", "hms_notified_faults TEXT")
