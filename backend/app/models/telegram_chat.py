"""Telegram chat model for multi-chat bot authorization."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from backend.app.core.database import Base

# Default notification events for new chats (when notify_events is NULL)
DEFAULT_NOTIFY_EVENTS = [
    "print_complete",
    "print_failed",
    "print_stopped",
    "print_paused",
    "plate_not_empty",
    "filament_deficit",
    "queue_job_waiting",
    "queue_job_skipped",
    "queue_job_failed",
    "printer_queue_completed",
    # Reports that auto-drying has STOPPED acting on a unit. Silence there reads
    # as "still drying", which is exactly how the reported re-arm loop went
    # unnoticed for two days — so an unconfigured chat hears it too.
    "ams_drying_suspended",
]

# All available notification event types
ALL_NOTIFY_EVENTS = [
    # Print lifecycle
    "print_start",
    "print_complete",
    "print_failed",
    "print_stopped",
    "print_progress",
    "print_paused",
    "print_resumed",
    "print_missing_spool_assignment",
    # Printer status
    "printer_offline",
    "printer_error",
    "ai_failure_detection",
    "filament_low",
    "filament_deficit",
    "maintenance_due",
    # AMS environmental
    "ams_humidity_high",
    "ams_temperature_high",
    "ams_ht_humidity_high",
    "ams_ht_temperature_high",
    # Reports that auto-drying has STOPPED acting, so it joins the defaults —
    # a chat that has never been configured still needs to hear this one.
    "ams_drying_suspended",
    # Zigbee sensor alerts (cycle A). Five here against two provider columns on
    # purpose: the column says whether at all, the chat says which ones — which
    # is m045's whole position. Deliberately NOT in DEFAULT_NOTIFY_EVENTS:
    # existing chats must not start receiving something new unasked.
    "sensor_above_max",
    "sensor_below_min",
    "sensor_back_in_range",
    "sensor_silent",
    "sensor_speaking_again",
    # Print events
    "plate_not_empty",
    "bed_cooled",
    "first_layer_complete",
    # Queue
    "queue_job_added",
    "queue_job_started",
    "queue_job_waiting",
    "queue_job_skipped",
    "queue_job_failed",
    "queue_completed",
    "printer_queue_completed",
    # Stock forecasting (upstream #1184). Live since m118/m119 —
    # ``stock_forecast_alerts`` fires both; see that service.
    "stock_reorder_alert",
    "stock_break_alert",
]


class TelegramChat(Base):
    """Telegram chat authorized to use the bot.

    Each chat has a group (role) that defines its permissions.
    Optionally linked to a system user for tracking.
    """

    __tablename__ = "telegram_chats"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Role - defines permissions. NULL only for auto-registered chats pending setup.
    group_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("groups.id", ondelete="RESTRICT"), nullable=True)

    # Optional link to system user (does NOT override group permissions)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=False)

    # Notification preferences - JSON list of enabled event types
    # NULL = use DEFAULT_NOTIFY_EVENTS, [] = none, [...] = custom selection
    notify_events: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    # Daily digest - receive daily summary
    daily_digest: Mapped[bool] = mapped_column(Boolean, default=False)

    # Per-chat floor for progress milestones (#28): NULL inherits the global
    # notify_progress_min_duration_minutes, 0 always sends, N mutes prints
    # estimated shorter than N minutes. Per chat because that is telegram's
    # whole authority model (m045) — an admin's 60-minute floor must not
    # decide for an operator's chat that wants 10.
    progress_min_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Quiet hours - suppress notifications during these hours
    quiet_hours_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    quiet_hours_start: Mapped[str | None] = mapped_column(String(5), nullable=True)  # HH:MM
    quiet_hours_end: Mapped[str | None] = mapped_column(String(5), nullable=True)  # HH:MM

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    group = relationship("Group", lazy="selectin")
    user = relationship("User", lazy="selectin")

    def get_permissions(self) -> set[str]:
        """Get permissions from the chat's assigned group."""
        if self.group:
            return set(self.group.permissions or [])
        return set()

    def has_permission(self, permission: str) -> bool:
        """Check if this chat has a specific permission."""
        return permission in self.get_permissions()

    def should_notify(self, event_type: str) -> bool:
        """Check if this chat should receive a notification for the given event."""
        if not self.is_active:
            return False
        if self.is_quiet_hours():
            return False
        events = self.notify_events if self.notify_events is not None else DEFAULT_NOTIFY_EVENTS
        return event_type in events

    def is_quiet_hours(self) -> bool:
        """Check if current time falls within quiet hours."""
        if not self.quiet_hours_enabled or not self.quiet_hours_start or not self.quiet_hours_end:
            return False
        from datetime import datetime

        now = datetime.now()
        try:
            start_h, start_m = map(int, self.quiet_hours_start.split(":"))
            end_h, end_m = map(int, self.quiet_hours_end.split(":"))
        except (ValueError, AttributeError):
            return False
        now_minutes = now.hour * 60 + now.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m
        if start_minutes <= end_minutes:
            return start_minutes <= now_minutes < end_minutes
        else:
            # Overnight: e.g. 22:00 - 07:00
            return now_minutes >= start_minutes or now_minutes < end_minutes

    def __repr__(self) -> str:
        return f"<TelegramChat {self.chat_id} label={self.label!r} active={self.is_active}>"
