"""Persist the default-on branded-profile/base-material routing rule."""

from backend.app.migrations.helpers import add_column

version = 174
name = "auto_queue_base_material_match"


async def upgrade(conn):
    await add_column(conn, "auto_queue_items", "allow_base_material_match BOOLEAN NOT NULL DEFAULT 1")
