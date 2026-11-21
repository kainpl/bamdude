"""G03 B2: record placement integrity and the separate history of unsent returns.

Older placements have no digest to prove they are unchanged. Leave them NULL.
Receipts address immutable identities and survive deletion of their rows, so
their numeric IDs are navigation hints without foreign keys. The schema here
is frozen independently of the ORM and safe to replay without backfill.
"""

from sqlalchemy import CheckConstraint, Column, DateTime, Integer, MetaData, String, Table, UniqueConstraint

from backend.app.migrations.helpers import add_column, json_column_type

version = 254
name = "auto_queue_unsent_returns"


async def upgrade(conn):
    await add_column(
        conn,
        "auto_queue_items",
        "unsent_return_count INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_aq_unsent_return_count "
        "CHECK (unsent_return_count >= 0)",
    )
    await add_column(conn, "auto_queue_items", f"unsent_return_proof {json_column_type()}")
    await add_column(conn, "print_queue", "placement_basis VARCHAR(72)")
    receipts = Table(
        "auto_queue_unsent_returns",
        MetaData(),
        Column("id", String(36), primary_key=True),
        Column("old_job_id", Integer, nullable=False),
        Column("old_job_identity", String(36), nullable=False),
        Column("old_queue_id", Integer, nullable=False),
        Column("execution_archive_id", Integer),
        Column("execution_archive_identity", String(36)),
        Column("auto_item_id", Integer, nullable=False),
        Column("auto_identity", String(36), nullable=False),
        Column("printer_id", Integer, nullable=False),
        Column("printer_identity", String(36), nullable=False),
        Column("failure_code", String(40), nullable=False),
        Column("ordinal", Integer, nullable=False),
        Column("returned_at", DateTime, nullable=False),
        UniqueConstraint("old_job_identity", name="uq_aq_unsent_return_job"),
        UniqueConstraint("auto_identity", "ordinal", name="uq_aq_unsent_return_auto_ordinal"),
        CheckConstraint("ordinal > 0", name="ck_aq_unsent_return_ordinal"),
    )
    await conn.run_sync(lambda sync: receipts.create(sync, checkfirst=True))
