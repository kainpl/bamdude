"""Scheduled AMS drying: a recurring rule and the concrete runs (vault 60-specs/scheduled-drying-spec).

Only ``services/scheduled_drying.py`` writes these tables. SQLite ignores ON
DELETE, so removing a printer deletes its rules and runs in code
(``scheduled_drying.forget_printer``); the FK actions are PostgreSQL's backstop.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class DryingSchedule(Base):
    """A recurring rule: dry this AMS at ``start_time`` (server time) on the chosen weekdays."""

    __tablename__ = "drying_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"))
    ams_id: Mapped[int] = mapped_column(Integer)
    temp: Mapped[int] = mapped_column(Integer)
    duration_hours: Mapped[int] = mapped_column(Integer)
    filament: Mapped[str] = mapped_column(String(50), default="")
    rotate_tray: Mapped[bool] = mapped_column(Boolean, default=False)
    # "HH:MM" in the SERVER's timezone (core/timezones.server_timezone()).
    start_time: Mapped[str] = mapped_column(String(5))
    # Monday = bit 0 … Sunday = bit 6; the day is the day a run STARTS.
    weekdays: Mapped[int] = mapped_column(Integer, default=127)
    # "HH:MM" or NULL; earlier than start_time means the next day.
    latest_start: Mapped[str | None] = mapped_column(String(5), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("ix_drying_schedules_printer", "printer_id"),)


class ScheduledDrying(Base):
    """One drying run — one-shot, or one occurrence of a rule."""

    __tablename__ = "scheduled_dryings"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"))
    ams_id: Mapped[int] = mapped_column(Integer)
    temp: Mapped[int] = mapped_column(Integer)
    duration_hours: Mapped[int] = mapped_column(Integer)
    filament: Mapped[str] = mapped_column(String(50), default="")
    rotate_tray: Mapped[bool] = mapped_column(Boolean, default=False)
    schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey("drying_schedules.id", ondelete="SET NULL"), nullable=True
    )
    # Naive UTC. start_after NULL = as soon as the printer is free.
    start_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    latest_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # pending / running / completed / cancelled / failed / skipped
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # Machine code: why a pending run waits, or why a failed/skipped one did not happen.
    reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_scheduled_dryings_status", "status"),
        Index("ix_scheduled_dryings_printer", "printer_id"),
    )
