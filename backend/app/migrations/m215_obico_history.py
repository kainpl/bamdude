"""Persist opt-in Obico observations with immutable file references (GH105).

Frames live in the managed obico-detections root. No process generation or
camera credentials belong in SQL. DDL is frozen at this migration step.
"""

from sqlalchemy import JSON, BigInteger, Column, DateTime, ForeignKey, Index, Integer, MetaData, String, Table

version = 215
name = "obico_history"


async def upgrade(conn):
    metadata = MetaData()
    Table("printers", metadata, Column("id", Integer, primary_key=True))
    events = Table(
        "obico_events",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("printer_id", Integer, ForeignKey("printers.id", ondelete="CASCADE"), nullable=False),
        Column("observed_at", DateTime, nullable=False),
        Column("verdict", String(8), nullable=False),
        Column("state", String(16), nullable=False),
        Column("size_bytes", BigInteger, nullable=False),
        Column("sha256", String(64), nullable=False),
        Column("width", Integer, nullable=False),
        Column("height", Integer, nullable=False),
        Column("facts", JSON, nullable=False),
        Column("raw_boxes", JSON, nullable=False),
        Column("invalid_boxes", Integer, nullable=False),
        Index("ix_obico_events_time", "observed_at", "id"),
        Index("ix_obico_events_printer_time", "printer_id", "observed_at", "id"),
        Index("ix_obico_events_retention", "verdict", "observed_at", "id"),
    )
    await conn.run_sync(lambda sync: events.create(sync, checkfirst=True))
