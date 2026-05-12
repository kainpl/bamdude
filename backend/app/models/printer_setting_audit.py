"""Audit row for one applied Printer Settings dialog change.

Written by backend/app/api/routes/printer_settings.py after each MQTT
publish (success → result='sent'; failure → result='error' +
error_message). Read by nobody yet — surfaced in a future viewer UI.

``tab`` discriminates which sub-dialog the change belongs to so a future
viewer can filter cheaply.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class PrinterSettingAudit(Base):
    __tablename__ = "printer_setting_audit"
    __table_args__ = (Index("ix_printer_setting_audit_printer", "printer_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    tab: Mapped[str] = mapped_column(String(30), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    sequence_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
