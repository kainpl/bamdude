"""The durable answer to one finished print's plate-clear question.

This is deliberately a receipt, not a second print-event stream.  Archive
facts may still be corrected later; the receipt records only which completion
card was answered and whether it cleared or re-armed its queue row.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class PrintCompletionReceipt(Base):
    __tablename__ = "print_completion_receipts"
    __table_args__ = (UniqueConstraint("archive_id", name="uq_print_completion_receipts_archive_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("print_archives.id", ondelete="CASCADE"), index=True)
    # The assessment is optional: Clear without submitting defects is a valid
    # action and must not falsely claim the operator graded the plate as zero.
    assessment: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    assessment_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    assessment_actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    plate_action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    plate_action_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    plate_action_actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    gate_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rearmed_queue_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
