"""Durable settlement/return facts, addressed by immutable execution identities.

These receipts outlive their jobs. Numeric ids are navigation hints, never FKs
that could cascade away the proof or attach it to a reused SQLite id.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class AutoQueueReturn(Base):
    __tablename__ = "auto_queue_returns"
    __table_args__ = (
        UniqueConstraint("old_job_identity", "execution_archive_identity", name="uq_aq_return_attempt_identity"),
        UniqueConstraint("auto_identity", "ordinal", name="uq_aq_return_auto_ordinal"),
        CheckConstraint(
            "(returned_at IS NULL AND ordinal IS NULL) OR "
            "(returned_at IS NOT NULL AND ordinal IS NOT NULL AND ordinal > 0)",
            name="ck_aq_return_publication",
        ),
        Index("ix_aq_return_ready_time", "returned_at", "ready_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    old_job_id: Mapped[int] = mapped_column(Integer)
    old_job_identity: Mapped[str] = mapped_column(String(36))
    old_queue_id: Mapped[int] = mapped_column(Integer)
    execution_archive_id: Mapped[int] = mapped_column(Integer)
    execution_archive_identity: Mapped[str] = mapped_column(String(36))
    auto_item_id: Mapped[int] = mapped_column(Integer)
    auto_identity: Mapped[str] = mapped_column(String(36))
    printer_id: Mapped[int] = mapped_column(Integer)
    printer_identity: Mapped[str] = mapped_column(String(36))
    owner_identity: Mapped[str] = mapped_column(String(36))
    source_basis: Mapped[dict] = mapped_column(JSON)
    source_basis_digest: Mapped[str] = mapped_column(String(64))
    dispatch_evidence: Mapped[dict] = mapped_column(JSON)
    completed_at: Mapped[datetime] = mapped_column(DateTime)
    ready_at: Mapped[datetime] = mapped_column(DateTime)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
