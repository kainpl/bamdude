"""Add ``source`` to firmware_batch_runs ('bulk' | 'single').

So the firmware update log can show whether a run came from the mass-update page
('bulk') or the per-printer firmware modal ('single'). New column on the existing
table — fresh installs get it via create_all (model field); this covers DBs that
already created the table under m081 without the column. Idempotent (add_column
guards on column_exists).
"""

from backend.app.migrations.helpers import add_column

version = 82
name = "firmware_batch_source"


async def upgrade(conn):
    await add_column(conn, "firmware_batch_runs", "source VARCHAR(16) DEFAULT 'bulk'")
