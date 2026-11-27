"""A count an app reports on its sidebar link (upstream Bambuddy 29a81520d, N45).

An app added to the sidebar as an external link can show a number on it, the way
the queue shows its waiting jobs: it reports the count for its own address with an
API key allowed to send notifications (``PUT /external-links/badge``). The count
lives in a table of its own, one row per link, so a report every minute leaves the
link's ``updated_at`` alone; a count not reported again for ten minutes is not shown.

``updated_at`` is stamped by the writer in UTC; no server default. No backfill.
"""

from backend.app.migrations.helpers import table_exists

version = 267
name = "external_link_badges"


async def upgrade(conn):
    ts = "DATETIME" if conn.dialect.name == "sqlite" else "TIMESTAMP"
    if not await table_exists(conn, "external_link_badges"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE external_link_badges (
                link_id INTEGER PRIMARY KEY REFERENCES external_links(id) ON DELETE CASCADE,
                count INTEGER NOT NULL DEFAULT 0,
                updated_at {ts} NOT NULL
            )
            """
        )
