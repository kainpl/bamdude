"""Drop the two identity leftovers the family catalog retired (spec A §6,
"later cleanup migration"):

- ``spool.resolved_filament_id`` — the FE-computed family of the pre-catalog
  era. Its VALUES are preserved first: copied into ``filament_family_id``
  wherever that is still NULL, because this migration runs BEFORE the startup
  backfill service and an upgrader jumping several versions would otherwise
  lose the only identity their spools had. The copy is plain SQL — migrations
  never touch the network or app services.
- ``slot_preset_mappings`` — the "remember what we sent" display table. Every
  reader and writer was removed in the 0.5.5 cycle (slot names come from the
  identity resolver now); the rows are worthless once nothing consults them.

Both operations are idempotent (``drop_column`` no-ops on a missing column;
``DROP TABLE IF EXISTS``) — ``DEBUG=true`` re-runs the newest migration on
every start.
"""

from backend.app.migrations.helpers import column_exists, drop_column

version = 150
name = "drop_retired_identity"


async def upgrade(conn):
    # Preserve, then drop.
    if await column_exists(conn, "spool", "resolved_filament_id"):
        await conn.exec_driver_sql(
            "UPDATE spool SET filament_family_id = resolved_filament_id"
            " WHERE filament_family_id IS NULL AND resolved_filament_id IS NOT NULL"
        )
    await drop_column(conn, "spool", "resolved_filament_id")

    await conn.exec_driver_sql("DROP TABLE IF EXISTS slot_preset_mappings")
