"""Retain the cloud preset owner for a deferred spool assignment."""

from backend.app.migrations.helpers import add_column

version = 257
name = "spool_assignment_preset_owner"


async def upgrade(conn):
    await add_column(conn, "spool_assignment", "preset_owner_identity VARCHAR(36)")
