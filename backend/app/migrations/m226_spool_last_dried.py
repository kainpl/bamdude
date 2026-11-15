"""When a spool was last dried (upstream 84f320b85).

An AMS drying cycle dries every spool in the unit, but the unit's humidity
describes the unit: once a spool is moved to another AMS or onto a shelf the
fact is gone. Three nullable columns on ``spool`` keep it with the spool:

* ``last_dried_at DATETIME`` — naive UTC, like the spool's other timestamps.
  Stamped by ``services/spool_drying`` when a drying cycle of the unit the spool
  is assigned to ends having run at least half its length (or with no more than
  five minutes left), or set by hand for a spool dried elsewhere.
* ``last_dried_temp INTEGER`` — the cycle's target °C, known only for a cycle
  BamDude started (the printer never reports it).
* ``last_dried_hours FLOAT`` — how long the cycle actually ran, known only when
  its length is (BamDude asked for it, or watched the countdown start).

A date set by hand clears the other two. No backfill: nothing recorded a drying
before this, so every existing spool starts with no record.
"""

from backend.app.migrations.helpers import add_column

version = 226
name = "spool_last_dried"


async def upgrade(conn):
    # PostgreSQL has no DATETIME type, and ``add_column`` sends the text as written.
    timestamp = "DATETIME" if conn.dialect.name == "sqlite" else "TIMESTAMP"
    await add_column(conn, "spool", f"last_dried_at {timestamp}")
    await add_column(conn, "spool", "last_dried_temp INTEGER")
    await add_column(conn, "spool", "last_dried_hours FLOAT")
