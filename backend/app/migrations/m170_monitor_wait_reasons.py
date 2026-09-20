"""Typed queue waits for the monitor, without interpreting legacy English text.

The scheduler owns these observations. Existing rows remain unknown until it
observes them again. Nullable columns keep fresh installs and upgrades aligned
and do not change readiness, routing or dispatch decisions.
"""

from backend.app.migrations.helpers import add_column

version = 170
name = "monitor_wait_reasons"


async def upgrade(conn):
    await add_column(conn, "print_queue", "waiting_reason_code VARCHAR(40)")
    await add_column(conn, "print_queue", "waiting_reason_checked_at TIMESTAMP")
