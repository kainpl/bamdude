"""Audit row for one applied AMS-settings change.

Written by ``backend/app/api/routes/ams_settings.py`` after each MQTT publish
(success → ``result='sent'``; failure → ``result='error'`` + ``error_message``).
Read by nobody yet — surfaced in a future viewer UI.

Why not just rely on Bambu Studio's behaviour: BS has no audit and no RBAC. On
a farm we gate this surface behind ``Permission.PRINTERS_UPDATE`` and want one
row per applied change so operators can answer "who turned RFID auto-read off?"
without diffing MQTT logs.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class AmsSettingAudit(Base):
    __tablename__ = "ams_setting_audit"
    __table_args__ = (Index("ix_ams_setting_audit_printer", "printer_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    sequence_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    result: Mapped[str] = mapped_column(String(20), nullable=False)  # 'sent' | 'error'
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
