"""Ownership of Notify! Live Activities and Lock Screen widgets (upstream Bambuddy v1.3.0, #3265).

A Notify! provider puts tiles and widgets on an iPhone through the Notify! Partner API; the remote
side knows them only by the ids it hands back (``LA…`` for a tile, ``WG…`` for a widget). Each row
here is BamDude's durable claim on one such remote resource, so a restart, a failed request or a
deleted provider can still end or delete exactly what this installation created, and an unconfirmed
start is never repeated blindly.

- ``notification_live_activities``: one row per provider, printer and print (``print_key``); ended
  and dismissed rows stay as tombstones so a dismissed tile is not shown again for that print.
- ``notification_lock_screen_widgets``: one widget per provider and printer.

``printer_id`` has no foreign key on purpose: the row outlives the printer until the remote
resource is closed, and it never grants anything. The provider owns its rows (``ON DELETE
CASCADE`` as a backstop; the provider delete removes them itself).

Tables and columns are named here: an ORM model would bring today's columns into a step of the
chain where they may not exist.
"""

from backend.app.migrations.helpers import table_exists

version = 269
name = "notify_ownership"


async def upgrade(conn):
    sqlite = conn.dialect.name == "sqlite"
    pk = "INTEGER PRIMARY KEY" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME" if sqlite else "TIMESTAMP"

    if not await table_exists(conn, "notification_live_activities"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE notification_live_activities (
                id {pk},
                provider_id INTEGER NOT NULL REFERENCES notification_providers (id) ON DELETE CASCADE,
                printer_id INTEGER NOT NULL,
                print_key VARCHAR(200) NOT NULL,
                job_key VARCHAR(160),
                print_name VARCHAR(255) NOT NULL,
                eta_seconds INTEGER,
                eta_deadline {ts},
                credential_key VARCHAR(64) NOT NULL,
                activity_id VARCHAR(16),
                state VARCHAR(20) NOT NULL,
                end_reason VARCHAR(40),
                failures INTEGER NOT NULL,
                content TEXT NOT NULL,
                expires_at {ts},
                next_attempt_at {ts},
                last_sent_at {ts},
                created_at {ts} NOT NULL,
                UNIQUE (provider_id, printer_id, print_key)
            )
            """
        )
    if not await table_exists(conn, "notification_lock_screen_widgets"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE notification_lock_screen_widgets (
                id {pk},
                provider_id INTEGER NOT NULL REFERENCES notification_providers (id) ON DELETE CASCADE,
                printer_id INTEGER NOT NULL,
                credential_key VARCHAR(64) NOT NULL,
                widget_id VARCHAR(16),
                state VARCHAR(20) NOT NULL,
                content TEXT NOT NULL,
                failures INTEGER NOT NULL,
                next_attempt_at {ts},
                last_sent_at {ts},
                created_at {ts} NOT NULL,
                UNIQUE (provider_id, printer_id)
            )
            """
        )
    for table in ("notification_live_activities", "notification_lock_screen_widgets"):
        for column in ("provider_id", "printer_id"):
            await conn.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})")
