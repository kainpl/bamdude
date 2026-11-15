"""Per-print usage history of Spoolman spools (upstream 1d86cff5a, #3407).

Spoolman keeps only a spool's totals, so in Spoolman mode nothing recorded
which print took how much from which spool — no History view, no usage list on
the spool, no trend for the stock forecast. ``spoolman_usage_history`` holds
one row per landed print charge.

A table of its own, deliberately not a ``spoolman_spool_id`` column on
``spool_usage_history`` (upstream's shape): the two inventories number their
spools independently, and one table with two key spaces is one forgotten
``IS NULL`` away from a Spoolman spool showing a built-in spool's prints
(upstream 704919339).

* ``source_token`` — which Spoolman the charge went to. The token lives in
  Settings (``spoolman_usage_source``), is created on first use and rotated
  when the Spoolman address changes; readers show the current token's rows.
* ``spool_registered`` — Spoolman's ``registered`` stamp of the spool at charge
  time: with the id it names ONE spool, so a spool Spoolman later gave the same
  id to does not inherit the rows.
* ``material`` … ``label_weight`` — the spool as it was, so a row reads the
  same after the spool is deleted in Spoolman.
* ``created_at`` is stamped by the writer in UTC; no server default.

No backfill: Spoolman has no per-print log to rebuild from, so history starts
with the first print after this migration.
"""

from backend.app.migrations.helpers import table_exists

version = 228
name = "spoolman_usage_history"


async def upgrade(conn):
    sqlite = conn.dialect.name == "sqlite"
    pk = "INTEGER PRIMARY KEY" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME" if sqlite else "TIMESTAMP"

    if not await table_exists(conn, "spoolman_usage_history"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE spoolman_usage_history (
                id {pk},
                source_token VARCHAR(32) NOT NULL,
                spoolman_spool_id INTEGER NOT NULL,
                spool_registered VARCHAR(40),
                printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL,
                print_name VARCHAR(500),
                archive_id INTEGER REFERENCES print_archives(id),
                weight_used FLOAT NOT NULL DEFAULT 0,
                percent_used INTEGER NOT NULL DEFAULT 0,
                status VARCHAR(20) NOT NULL DEFAULT 'completed',
                cost FLOAT,
                created_at {ts} NOT NULL,
                material VARCHAR(100),
                subtype VARCHAR(200),
                brand VARCHAR(200),
                color_name VARCHAR(200),
                rgba VARCHAR(8),
                slicer_filament_name VARCHAR(200),
                label_weight FLOAT
            )
            """
        )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_spoolman_usage_history_source_created "
        "ON spoolman_usage_history (source_token, created_at)"
    )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_spoolman_usage_history_spool "
        "ON spoolman_usage_history (source_token, spoolman_spool_id, created_at)"
    )
