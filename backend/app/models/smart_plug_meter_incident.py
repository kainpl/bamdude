"""One meter-stall episode of a plug's measurement source (m211, spec plug-meter-watchdog W6).

Opened when the sweep or a print's energy proves a flat counter under real load; closed by the next
increase after the last flat point (``recovered``), or when the source changes or the plug goes
(``source_changed`` / ``removed``) -- never by silence. ``notified`` is set in the same commit as the
inbox rows, so a restart neither loses nor repeats the warning. The partial unique index allows one
active episode per plug.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class SmartPlugMeterIncident(Base):
    __tablename__ = "smart_plug_meter_incidents"
    __table_args__ = (
        Index(
            "uq_meter_incidents_active",
            "plug_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_meter_incidents_plug", "plug_id", "opened_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plug_id: Mapped[int] = mapped_column(ForeignKey("smart_plugs.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # The flat series: its first and (so far) last counter point, and the value it held.
    flat_since: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    flat_until: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    total_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
