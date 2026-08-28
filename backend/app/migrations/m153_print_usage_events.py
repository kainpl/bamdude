"""m153: print_usage_events journal + notification_providers.on_filament_runout.

The journal is the persisted per-print attribution record (spool ids frozen at
event time); it supersedes the JSON ``active_print_sessions.tray_change_log``
column, which stays in place but is no longer written (readers keep a one-release
fallback for a print running across the upgrade). Retention: rows are pruned by
the ``usage_events_retention_hours`` sweep in main.py's cleanup loop and by the
archive hard-delete path — the FK CASCADE is honoured by PostgreSQL only.

Fresh installs get the table and the column from ``create_all()``; every DDL
statement here is guarded so the migration no-ops there.
"""

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import add_column, column_exists, table_exists

version = 153
name = "print_usage_events"


async def upgrade(conn):
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME" if sqlite else "TIMESTAMP"

    if not await table_exists(conn, "print_usage_events"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE print_usage_events (
                id {pk},
                printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                archive_id INTEGER NOT NULL REFERENCES print_archives(id) ON DELETE CASCADE,
                layer_num INTEGER NOT NULL,
                event VARCHAR(24) NOT NULL,
                kind VARCHAR(24),
                global_tray_id INTEGER,
                spool_id INTEGER,
                spoolman_spool_id INTEGER,
                created_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.exec_driver_sql("CREATE INDEX ix_print_usage_events_printer_id ON print_usage_events (printer_id)")
        await conn.exec_driver_sql("CREATE INDEX ix_print_usage_events_archive_id ON print_usage_events (archive_id)")

    if not await column_exists(conn, "notification_providers", "on_filament_runout"):
        await add_column(conn, "notification_providers", "on_filament_runout BOOLEAN DEFAULT 0")
