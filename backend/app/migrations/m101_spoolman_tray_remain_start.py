"""Add active_print_spoolman.tray_remain_start (upstream Bambuddy #1820 / e09a33be).

Per-slot AMS remain% + tray_uuid at print start so the completion path can write
filament consumption for no-3MF "Untitled" prints (A6/A7 fallback archives) via an
AMS remain%-delta, mirroring internal-inventory Path 2 (usage_tracker).

Adapted vs upstream: we do NOT relax ``filament_usage`` NOT NULL. store_print_data
persists ``[]`` for the no-3MF case (report paths read ``filament_usage or []`` so
``[]`` is behaviorally identical to NULL), which lets this migration be a single
nullable column add — no fragile SQLite ``PRAGMA writable_schema`` rewrite.

Fresh installs get the column from the model's create_all; this backfills existing
DBs. Idempotent via column_exists.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import column_exists

version = 101
name = "spoolman_tray_remain_start"


async def upgrade(conn):
    if await column_exists(conn, "active_print_spoolman", "tray_remain_start"):
        return
    # JSON columns are stored as TEXT on SQLite (the SQLAlchemy JSON type
    # serialises to a TEXT affinity); Postgres has a native JSON type.
    coltype = "JSON" if is_postgres() else "TEXT"
    await conn.execute(text(f"ALTER TABLE active_print_spoolman ADD COLUMN tray_remain_start {coltype}"))
