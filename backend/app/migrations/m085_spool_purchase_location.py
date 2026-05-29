"""Add ``purchase_location`` to the ``spool`` table.

A free-form "where I bought this" label, distinct from ``storage_location``
(where the spool is physically kept). Surfaced in the spool form, the bulk-edit
dialog, and as an optional inventory column. Nullable, no default — existing
spools render blank until set.
"""

from backend.app.migrations.helpers import add_column

version = 85
name = "spool_purchase_location"


async def upgrade(conn):
    await add_column(conn, "spool", "purchase_location VARCHAR(255)")
