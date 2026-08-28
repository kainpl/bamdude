"""Notification provider and log models for push notifications."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from backend.app.core.database import Base


class NotificationDigestQueue(Base):
    """Model for queuing notifications to be sent in daily digest."""

    __tablename__ = "notification_digest_queue"

    id = Column(Integer, primary_key=True, index=True)
    provider_id = Column(Integer, ForeignKey("notification_providers.id", ondelete="CASCADE"), nullable=False)
    event_type = Column(String(50), nullable=False)  # print_start, print_complete, etc.
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    printer_id = Column(Integer, ForeignKey("printers.id", ondelete="SET NULL"), nullable=True)
    printer_name = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    # Relationships
    provider = relationship("NotificationProvider", back_populates="digest_queue")


class NotificationLog(Base):
    """Model for logging sent notifications."""

    __tablename__ = "notification_logs"

    id = Column(Integer, primary_key=True, index=True)
    provider_id = Column(Integer, ForeignKey("notification_providers.id", ondelete="CASCADE"), nullable=False)
    event_type = Column(String(50), nullable=False)  # print_start, print_complete, etc.
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    success = Column(Boolean, default=True)
    error_message = Column(Text, nullable=True)
    printer_id = Column(Integer, ForeignKey("printers.id", ondelete="SET NULL"), nullable=True)
    printer_name = Column(String(100), nullable=True)  # Store name in case printer is deleted
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    # Relationships
    provider = relationship("NotificationProvider", back_populates="logs")


# One registry, one source of truth: event key -> default subscription for a
# NEW provider. The API schema mirrors these defaults (pinned by a test); the
# order is the order the events were born in, purely cosmetic.
PROVIDER_EVENT_DEFAULTS: dict[str, bool] = {
    "on_print_start": False,
    "on_print_complete": True,
    "on_print_failed": True,
    "on_print_stopped": True,
    "on_print_progress": False,
    "on_print_missing_spool_assignment": False,
    "on_print_paused": True,
    "on_print_resumed": True,
    "on_printer_offline": False,
    "on_printer_error": False,
    "on_ai_failure_detection": False,
    "on_filament_low": False,
    "on_filament_runout": False,
    "on_filament_deficit": True,
    "on_maintenance_due": False,
    "on_ams_humidity_high": False,
    "on_ams_temperature_high": False,
    "on_ams_drying_suspended": True,
    "on_ams_ht_humidity_high": False,
    "on_ams_ht_temperature_high": False,
    "on_sensor_threshold": False,
    "on_sensor_silent": False,
    "on_plate_not_empty": True,
    "on_bed_cooled": False,
    "on_first_layer_complete": False,
    "on_queue_job_added": False,
    "on_queue_job_started": False,
    "on_queue_job_waiting": True,
    "on_queue_job_skipped": True,
    "on_queue_job_failed": True,
    "on_queue_completed": False,
    "on_printer_queue_completed": True,
    "on_stock_reorder_alert": False,
    "on_stock_break_alert": False,
}


class NotificationProvider(Base):
    """Model for notification providers (WhatsApp, ntfy, Pushover, etc.)."""

    __tablename__ = "notification_providers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)  # User-defined name
    provider_type = Column(String(50), nullable=False)  # callmebot, ntfy, pushover, telegram, email
    enabled = Column(Boolean, default=True)

    # Provider-specific configuration stored as JSON string
    config = Column(Text, nullable=False)

    # Which events this provider wants — ONE JSON list of event keys
    # (``on_print_start``-style names, matching the API contract), replacing
    # 34 boolean columns that grew one migration at a time (m157). NULL =
    # the defaults in ``PROVIDER_EVENT_DEFAULTS``. Adding a new event is now
    # a registry row + a schema field — never a column, never DDL.
    # Ignored for telegram: per-chat ``notify_events`` is the authority
    # there (m045), and the dispatch includes telegram providers without
    # consulting this list at all.
    subscribed_events = Column(JSON, nullable=True)

    # Per-provider floor for progress milestones (#28): NULL or 0 = always
    # send, N mutes prints estimated shorter than N minutes. Ignored for
    # telegram, whose authority is the chat (each chat carries its own floor).
    # There is no global fallback: every provider carries its own value.
    progress_min_duration_minutes = Column(Integer, nullable=True)

    # Quiet hours (do not disturb)
    quiet_hours_enabled = Column(Boolean, default=False)
    quiet_hours_start = Column(String(5), nullable=True)  # HH:MM format, e.g., "22:00"
    quiet_hours_end = Column(String(5), nullable=True)  # HH:MM format, e.g., "07:00"

    # Daily digest (batch notifications into a single daily summary)
    daily_digest_enabled = Column(Boolean, default=False)
    daily_digest_time = Column(String(5), nullable=True)  # HH:MM format, e.g., "08:00"

    # Optional: Link to specific printer (NULL = all printers)
    printer_id = Column(Integer, ForeignKey("printers.id", ondelete="SET NULL"), nullable=True)

    # Status tracking
    last_success = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    last_error_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    printer = relationship("Printer", back_populates="notification_providers")
    logs = relationship("NotificationLog", back_populates="provider", cascade="all, delete-orphan")

    def wants_event(self, event_field: str) -> bool:
        """Does this provider subscribe to an event (``on_*`` key)?

        NULL = the registry defaults; a stored list is an explicit full
        selection, so an event ABSENT from it is off — including events born
        after the list was saved (the operator has a custom selection; a new
        event joining it unasked is the m045-era failure mode).
        """
        if self.subscribed_events is None:
            return PROVIDER_EVENT_DEFAULTS.get(event_field, False)
        return event_field in self.subscribed_events

    def events_map(self) -> dict[str, bool]:
        """Every known event as the boolean the API contract promises."""
        return {field: self.wants_event(field) for field in PROVIDER_EVENT_DEFAULTS}

    def set_event(self, event_field: str, value: bool) -> None:
        """Flip one event, materialising the defaults into an explicit list."""
        current = (
            set(self.subscribed_events)
            if self.subscribed_events is not None
            else {f for f, d in PROVIDER_EVENT_DEFAULTS.items() if d}
        )
        if value:
            current.add(event_field)
        else:
            current.discard(event_field)
        self.subscribed_events = sorted(current & set(PROVIDER_EVENT_DEFAULTS))

    digest_queue = relationship("NotificationDigestQueue", back_populates="provider", cascade="all, delete-orphan")


# ⚠️ LEGACY SCHEMA SHIM — the 23 event columns m045 (frozen) UPDATEs BY NAME.
# A fresh install replays the whole migration chain against the create_all
# schema, so these columns must EXIST when m045 runs — and m157 drops them at
# its own point in the chain, on fresh installs and upgrades alike. They are
# appended to the TABLE only, never mapped: no attribute, no reader, no
# writer. The post-m045 event columns are absent here on purpose — their
# migrations start with ``add_column`` and self-heal. Do not "clean this up"
# before the migration chain itself is re-baselined.
_M045_SHIM_COLUMNS = (
    "on_print_start",
    "on_print_complete",
    "on_print_failed",
    "on_print_stopped",
    "on_print_progress",
    "on_print_missing_spool_assignment",
    "on_printer_offline",
    "on_printer_error",
    "on_filament_low",
    "on_maintenance_due",
    "on_ams_humidity_high",
    "on_ams_temperature_high",
    "on_ams_ht_humidity_high",
    "on_ams_ht_temperature_high",
    "on_plate_not_empty",
    "on_bed_cooled",
    "on_first_layer_complete",
    "on_queue_job_added",
    "on_queue_job_started",
    "on_queue_job_waiting",
    "on_queue_job_skipped",
    "on_queue_job_failed",
    "on_queue_completed",
)
for _shim_name in _M045_SHIM_COLUMNS:
    NotificationProvider.__table__.append_column(Column(_shim_name, Boolean))
