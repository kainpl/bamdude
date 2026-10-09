"""Keep camera opt-out explicit and per-job; existing work retains the 1% check.

Nullable JSON lets old rows use safe defaults without backfilling a mutable
order preference into existing jobs. Copies carry their original policy.
"""

from backend.app.migrations.helpers import add_column, json_column_type

version = 198
name = "auto_eject_camera_settings"


async def upgrade(conn):
    for table in ("projects", "print_queue", "auto_queue_items"):
        await add_column(conn, table, f"auto_eject_settings {json_column_type()}")
