"""Stage failed-return facts without enabling a writer or inferring old settlement.

The old failed printer row keeps its failure code and archive. Its separate
marker addresses a receipt by immutable identity. Receipts intentionally have
no numeric foreign keys: deleting a job/ancestor/printer must not erase proof
or let a reused SQLite id inherit it. Ready receipts have no ordinal; publication
sets returned_at and a positive ordinal together. Both uniqueness constraints
use names distinct from the pass's lookup index (SQLite -> PG, d525d5663).

Schema is frozen here, independent of future ORM defaults. Upgrade and DEBUG
reruns never backfill receipts or overwrite an existing toggle/proof/count.
"""

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    text,
)

from backend.app.migrations.helpers import add_column, json_column_type

version = 226
name = "auto_queue_return_facts"


def _receipt_table():
    return Table(
        "auto_queue_returns",
        MetaData(),
        Column("id", String(36), primary_key=True),
        Column("old_job_id", Integer, nullable=False),
        Column("old_job_identity", String(36), nullable=False),
        Column("old_queue_id", Integer, nullable=False),
        Column("execution_archive_id", Integer, nullable=False),
        Column("execution_archive_identity", String(36), nullable=False),
        Column("auto_item_id", Integer, nullable=False),
        Column("auto_identity", String(36), nullable=False),
        Column("printer_id", Integer, nullable=False),
        Column("printer_identity", String(36), nullable=False),
        Column("owner_identity", String(36), nullable=False),
        Column("source_basis", JSON, nullable=False),
        Column("source_basis_digest", String(64), nullable=False),
        Column("dispatch_evidence", JSON, nullable=False),
        Column("completed_at", DateTime, nullable=False),
        Column("ready_at", DateTime, nullable=False),
        Column("returned_at", DateTime, nullable=True),
        Column("ordinal", Integer, nullable=True),
        UniqueConstraint("old_job_identity", "execution_archive_identity", name="uq_aq_return_attempt_identity"),
        UniqueConstraint("auto_identity", "ordinal", name="uq_aq_return_auto_ordinal"),
        CheckConstraint(
            "(returned_at IS NULL AND ordinal IS NULL) OR "
            "(returned_at IS NOT NULL AND ordinal IS NOT NULL AND ordinal > 0)",
            name="ck_aq_return_publication",
        ),
        Index("ix_aq_return_ready_time", "returned_at", "ready_at", "id"),
    )


async def upgrade(conn):
    await add_column(
        conn,
        "auto_queue_items",
        "failed_return_count INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_aq_return_count CHECK (failed_return_count >= 0)",
    )
    await add_column(conn, "auto_queue_items", f"failed_return_proof {json_column_type()}")
    await add_column(conn, "print_queue", "returned_receipt_identity VARCHAR(36)")
    receipt = _receipt_table()
    await conn.run_sync(lambda sync: receipt.create(sync, checkfirst=True))
    # A retry after an interrupted SQLite DDL must also restore a missing index.
    for index in receipt.indexes:
        await conn.run_sync(lambda sync, index=index: index.create(sync, checkfirst=True))
    await conn.execute(
        text(
            "INSERT INTO settings (key, value) VALUES ('auto_queue_return_failed', 'false') ON CONFLICT (key) DO NOTHING"
        )
    )
