from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, func, or_
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base

AMS_SYNC_STATUS = "ams_sync"
RUNOUT_STATUS = "runout"
SERVICE_USAGE_STATUSES = frozenset({AMS_SYNC_STATUS, RUNOUT_STATUS})


class SpoolUsageHistory(Base):
    """Print consumption and explicitly typed lifetime corrections for a spool."""

    __tablename__ = "spool_usage_history"
    __table_args__ = (
        # The forecast engine's window scans — day-bucketed grams and the
        # newest event per spool — all enter through (spool_id, created_at).
        # Existing installs get it via m159; fresh installs via create_all.
        Index("ix_spool_usage_history_spool_created", "spool_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    spool_id: Mapped[int] = mapped_column(ForeignKey("spool.id", ondelete="CASCADE"))
    printer_id: Mapped[int | None] = mapped_column(ForeignKey("printers.id", ondelete="SET NULL"))
    print_name: Mapped[str | None] = mapped_column(String(500))
    archive_id: Mapped[int | None] = mapped_column(ForeignKey("print_archives.id", ondelete="SET NULL"), nullable=True)
    weight_used: Mapped[float] = mapped_column(Float, default=0)
    percent_used: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="completed")  # print outcome or service correction
    cost: Mapped[float | None] = mapped_column(Float)  # Calculated cost for this usage event
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    @property
    def archive_weight_used(self) -> float:
        """Only print rows contributed to their archive; corrections did not.

        The full weight still belongs to the spool's lifetime ledger. Names,
        archive links and the current spool balance cannot classify a row.
        """
        return 0.0 if self.status in SERVICE_USAGE_STATUSES else self.weight_used or 0.0

    @classmethod
    def print_contribution_filter(cls):
        """Keep legacy/unknown print outcomes; omit known lifetime corrections."""
        return or_(cls.status.is_(None), cls.status.notin_(SERVICE_USAGE_STATUSES))
