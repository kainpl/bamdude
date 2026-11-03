"""A saved ML observation, never authority to change a physical print."""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class ObicoEvent(Base):
    __tablename__ = "obico_events"
    __table_args__ = (
        Index("ix_obico_events_time", "observed_at", "id"),
        Index("ix_obico_events_printer_time", "printer_id", "observed_at", "id"),
        Index("ix_obico_events_retention", "verdict", "observed_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    verdict: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    facts: Mapped[dict] = mapped_column(JSON, nullable=False)
    raw_boxes: Mapped[list] = mapped_column(JSON, nullable=False)
    invalid_boxes: Mapped[int] = mapped_column(Integer, nullable=False)
