"""Connector identities and the connector's submission receipts (OrcaBamDude spec r2 §8.1, addendum C3).

``api_keys.connector_identity`` and ``printers.connector_identity`` name ONE record for good: a random UUID per row,
never copied and never one shared default. SQLite hands a deleted row's numeric id to the next row, and a serial
number is treated as sensitive, so a slicer connector's receipts and target memory need a name that is neither.

Existing SQLite databases get a nullable column, a per-row UUID backfill and then a unique index. ``printers`` and
``api_keys`` are NOT rebuilt only to declare NOT NULL. The connector refuses a missing identity at its own boundary.
PostgreSQL also gets NOT NULL after the backfill. A fresh install gets the model's non-null unique column and its
per-row default from ``create_all``.

The two receipt tables and the server's connector identity and storage epoch (Settings) are created here too. The
epoch is stable across a normal restart; a restore rotates it before the connector opens again.

DDL and seed values are frozen at this step: lightweight table definitions, never today's ORM tables.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    text,
)

from backend.app.migrations.helpers import add_column, column_exists, table_exists

version = 216
name = "connector_identity"

SERVER_ID_KEY = "connector_server_id"
STORAGE_EPOCH_KEY = "connector_storage_epoch"
IDENTITY_TABLES = ("api_keys", "printers")


def _receipt_tables() -> tuple[Table, Table]:
    metadata = MetaData()
    submissions = Table(
        "connector_submissions",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("operation_id", String(36), nullable=False),
        Column("api_key_identity", String(36), nullable=False, index=True),
        Column("owner_id", Integer, nullable=True),
        Column("server_id", String(36), nullable=False),
        Column("storage_epoch", String(36), nullable=False),
        Column("manifest", JSON, nullable=False),
        Column("manifest_hash", String(64), nullable=False),
        Column("state", String(16), nullable=False),
        Column("cancel_requested", Boolean, nullable=False),
        Column("compacted_at", DateTime, nullable=True),
        Column("created_at", DateTime, nullable=False),
        Column("updated_at", DateTime, nullable=False),
        UniqueConstraint(
            "server_id", "storage_epoch", "api_key_identity", "operation_id", name="uq_connector_submission_namespace"
        ),
        sqlite_autoincrement=True,
    )
    entries = Table(
        "connector_submission_entries",
        metadata,
        Column("id", Integer, primary_key=True),
        Column(
            "submission_id",
            Integer,
            ForeignKey("connector_submissions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        Column("entry_id", String(36), nullable=False),
        Column("ordinal", Integer, nullable=False),
        Column("intent_hash", String(64), nullable=False),
        Column("state", String(20), nullable=False),
        Column("published_receipt", JSON, nullable=True),
        Column("error_code", String(64), nullable=True),
        Column("attempt_generation", Integer, nullable=False),
        Column("created_at", DateTime, nullable=False),
        Column("updated_at", DateTime, nullable=False),
        UniqueConstraint("submission_id", "entry_id", name="uq_connector_submission_entry"),
        sqlite_autoincrement=True,
    )
    return submissions, entries


async def _give_each_row_an_identity(conn, table: str) -> None:
    await add_column(conn, table, "connector_identity VARCHAR(36)")
    rows = (
        await conn.execute(text(f"SELECT id FROM {table} WHERE connector_identity IS NULL OR connector_identity = ''"))
    ).all()
    for (row_id,) in rows:
        await conn.execute(
            text(f"UPDATE {table} SET connector_identity = :identity WHERE id = :id"),
            {"identity": str(uuid.uuid4()), "id": row_id},
        )
    await conn.execute(
        text(f"CREATE UNIQUE INDEX IF NOT EXISTS ix_{table}_connector_identity ON {table} (connector_identity)")
    )
    if conn.dialect.name == "postgresql":
        await conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN connector_identity SET NOT NULL"))


async def _seed_setting(conn, key: str) -> None:
    exists = (await conn.execute(text("SELECT 1 FROM settings WHERE key = :key"), {"key": key})).first()
    if exists is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        await conn.execute(
            text("INSERT INTO settings (key, value, created_at, updated_at) VALUES (:key, :value, :now, :now)"),
            {"key": key, "value": str(uuid.uuid4()), "now": now},
        )


async def upgrade(conn):
    for table in IDENTITY_TABLES:
        if await table_exists(conn, table):
            await _give_each_row_an_identity(conn, table)
    submissions, entries = _receipt_tables()
    await conn.run_sync(lambda sync: submissions.metadata.create_all(sync, checkfirst=True))
    if await table_exists(conn, "settings") and await column_exists(conn, "settings", "key"):
        for key in (SERVER_ID_KEY, STORAGE_EPOCH_KEY):
            await _seed_setting(conn, key)
