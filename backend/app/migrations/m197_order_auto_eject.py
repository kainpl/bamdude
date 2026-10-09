"""Order preference and per-job snapshots; existing work remains manual."""

from backend.app.migrations.helpers import add_column

version = 197
name = "order_auto_eject"


async def upgrade(conn):
    await add_column(conn, "projects", "auto_eject_enabled BOOLEAN NOT NULL DEFAULT FALSE")
    await add_column(conn, "print_queue", "auto_eject BOOLEAN NOT NULL DEFAULT FALSE")
    await add_column(conn, "auto_queue_items", "auto_eject BOOLEAN NOT NULL DEFAULT FALSE")
