"""Give each local spool a stable row identity for its printed QR labels."""

from uuid import uuid4

from sqlalchemy import text

from backend.app.migrations.helpers import add_column

version = 260
name = "spool_label_identity"


async def upgrade(conn):
    await add_column(conn, "spool", "label_identity VARCHAR(36)")
    while True:
        ids = (await conn.execute(text("SELECT id FROM spool WHERE label_identity IS NULL LIMIT 500"))).scalars().all()
        if not ids:
            break
        await conn.execute(
            text("UPDATE spool SET label_identity = :identity WHERE id = :id AND label_identity IS NULL"),
            [{"id": row_id, "identity": str(uuid4())} for row_id in ids],
        )
    await conn.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ix_spool_label_identity ON spool (label_identity)")
