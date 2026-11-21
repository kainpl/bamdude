"""Remove m252's backfill default from PostgreSQL once the column exists.

m252 needed false to add the non-null column to populated databases. Fresh
schemas use the ORM default instead. Remove only the known backfill default
so imported PostgreSQL schemas match fresh installs and preserve key values.
"""

from sqlalchemy import text

version = 255
name = "notification_permission_default"


async def upgrade(conn):
    if conn.dialect.name != "postgresql":
        return
    default = await conn.scalar(
        text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='api_keys' AND column_name='can_send_notifications'"
        )
    )
    if default in ("false", "'false'::boolean"):
        await conn.exec_driver_sql("ALTER TABLE public.api_keys ALTER COLUMN can_send_notifications DROP DEFAULT")


async def replay(conn):
    await upgrade(conn)
