"""ORM for calibration_audit (m062).

Mirrors ams_setting_audit (m060) + printer_setting_audit (m061) pattern.
One row per user-initiated action: start_session, save_result,
sync_printer, delete, set_active, cancel. Written by routes after MQTT
publish + DB write.

UI viewer not provided in phase-1; query the table directly.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class CalibrationAudit(Base):
    __tablename__ = "calibration_audit"
    __table_args__ = (Index("ix_calibration_audit_printer", "printer_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibration_session.id", ondelete="SET NULL"), nullable=True
    )
    filament_calibration_id: Mapped[int | None] = mapped_column(
        ForeignKey("filament_calibration.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    sequence_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
