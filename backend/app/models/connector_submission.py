"""A slicer connector's submission and its entries -- the durable receipt of new print work (OrcaBamDude spec r2 §8).

An operation is addressed by its client UUID inside the namespace of the API key's immutable ``connector_identity``
and the server's identity and storage epoch, never by the numeric key or user id (both are reused on SQLite).

What makes an entry published is ``published_receipt``: it is written in the same transaction as the job rows it
names, so a committed receipt always has its jobs and a job never lacks its receipt. The ``state`` columns are
diagnostic -- every reader asks the receipt, and every write of a non-publication state is a compare-and-set on
``published_receipt IS NULL``.

Queue ids in a receipt are historical scalars, deliberately not foreign keys: cleaning a finished job must never
erase the proof that it was published.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class ConnectorSubmission(Base):
    __tablename__ = "connector_submissions"
    __table_args__ = (
        UniqueConstraint(
            "server_id", "storage_epoch", "api_key_identity", "operation_id", name="uq_connector_submission_namespace"
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    api_key_identity: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # The key's owner when the operation was made -- audit only, never what grants access to it.
    owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    server_id: Mapped[str] = mapped_column(String(36), nullable=False)
    storage_epoch: Mapped[str] = mapped_column(String(36), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="processing")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    compacted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ConnectorSubmissionEntry(Base):
    __tablename__ = "connector_submission_entries"
    __table_args__ = (
        UniqueConstraint("submission_id", "entry_id", name="uq_connector_submission_entry"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("connector_submissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entry_id: Mapped[str] = mapped_column(String(36), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    intent_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    published_receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
