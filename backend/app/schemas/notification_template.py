"""Pydantic schemas for notification templates."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from backend.app.schemas.not_null import IgnoresNullForRequiredColumns


class EventType(StrEnum):
    """Supported notification event types."""

    PRINT_START = "print_start"
    PRINT_COMPLETE = "print_complete"
    PRINT_FAILED = "print_failed"
    PRINT_STOPPED = "print_stopped"
    PRINT_PAUSED = "print_paused"
    PRINT_RESUMED = "print_resumed"
    PRINT_PROGRESS = "print_progress"
    PRINT_MISSING_SPOOL_ASSIGNMENT = "print_missing_spool_assignment"
    PRINTER_OFFLINE = "printer_offline"
    PRINTER_ERROR = "printer_error"
    AI_FAILURE_DETECTION = "ai_failure_detection"
    FILAMENT_LOW = "filament_low"
    MAINTENANCE_DUE = "maintenance_due"
    AMS_HUMIDITY_HIGH = "ams_humidity_high"
    AMS_TEMPERATURE_HIGH = "ams_temperature_high"
    BED_COOLED = "bed_cooled"
    TEST = "test"


# Available variables for each event type
EVENT_VARIABLES: dict[str, list[str]] = {
    "print_start": ["printer", "filename", "estimated_time", "eta", "timestamp", "app_name"],
    "print_complete": [
        "printer",
        "filename",
        "duration",
        "filament_grams",
        "filament_details",
        "finish_photo_url",
        "timestamp",
        "app_name",
    ],
    "print_failed": [
        "printer",
        "filename",
        "duration",
        "filament_grams",
        "filament_details",
        "progress",
        "reason",
        "finish_photo_url",
        "timestamp",
        "app_name",
    ],
    "print_stopped": [
        "printer",
        "filename",
        "duration",
        "filament_grams",
        "filament_details",
        "progress",
        "finish_photo_url",
        "timestamp",
        "app_name",
    ],
    "print_paused": [
        "printer",
        "filename",
        "reason",
        "reason_code",
        "hms_code",
        "timestamp",
        "app_name",
    ],
    "print_resumed": ["printer", "filename", "paused_for", "timestamp", "app_name"],
    "print_progress": ["printer", "filename", "progress", "remaining_time", "eta", "timestamp", "app_name"],
    "print_missing_spool_assignment": [
        "printer",
        "missing_slots",
        "missing_slot_details",
        "timestamp",
        "app_name",
    ],
    "print_usage_not_recorded": [
        "printer",
        "missing_slots",
        "missing_slot_details",
        "timestamp",
        "app_name",
    ],
    "print_usage_spool_changed": [
        "printer",
        "missing_slots",
        "missing_slot_details",
        "timestamp",
        "app_name",
    ],
    "printer_offline": ["printer", "timestamp", "app_name"],
    "printer_error": ["printer", "error_type", "error_detail", "timestamp", "app_name"],
    "ai_failure_detection": [
        "printer",
        "task_name",
        "provider",
        "print_quality",
        "confidence",
        "action",
        "timestamp",
        "app_name",
    ],
    "filament_low": ["printer", "slot", "remaining_percent", "color", "timestamp", "app_name"],
    "maintenance_due": ["printer", "items", "timestamp", "app_name"],
    "ams_humidity_high": ["printer", "ams_label", "humidity", "threshold", "timestamp", "app_name"],
    "ams_temperature_high": ["printer", "ams_label", "temperature", "threshold", "timestamp", "app_name"],
    "bed_cooled": ["printer", "bed_temp", "threshold", "filename", "timestamp", "app_name"],
    "pause_coming_up": [
        "printer",
        "filename",
        "layer",
        "total_layers",
        "time_to_pause",
        "minutes",
        "timestamp",
        "app_name",
    ],
    "print_outcome_unchecked": ["printer", "filename", "finished_at", "archive_url", "timestamp", "app_name"],
    # Events whose templates are editable like the rest (the variables each default template uses).
    "plate_not_empty": ["printer", "timestamp", "app_name"],
    "filament_deficit": ["printer", "print_name", "needed", "slot", "available", "missing", "timestamp", "app_name"],
    "ams_drying_suspended": ["printer", "ams_label", "cycles", "humidity", "threshold", "timestamp", "app_name"],
    "ams_drying_not_heating": ["printer", "ams_label", "cycles", "humidity", "threshold", "timestamp", "app_name"],
    "scheduled_drying_started": [
        "printer",
        "ams_label",
        "filament",
        "temp",
        "hours",
        "schedule",
        "timestamp",
        "app_name",
    ],
    "scheduled_drying_completed": ["printer", "ams_label", "hours", "temp", "schedule", "timestamp", "app_name"],
    "scheduled_drying_failed": ["printer", "ams_label", "reason", "schedule", "timestamp", "app_name"],
    "first_layer_complete": ["printer", "filename", "total_layers", "timestamp", "app_name"],
    "stock_reorder_alert": ["filament", "brand", "stock_g", "rate_g_day", "days_left", "timestamp", "app_name"],
    "stock_break_alert": [
        "filament",
        "brand",
        "stock_g",
        "rate_g_day",
        "lead_time_days",
        "days_left",
        "timestamp",
        "app_name",
    ],
    "stock_depleted_alert": ["filament", "brand", "rate_g_day", "lead_time_days", "timestamp", "app_name"],
    "external_app_message": ["title", "message", "level", "level_icon", "link", "sender", "timestamp", "app_name"],
    "sensor_above_max": ["location", "quantity", "value", "unit", "limit", "timestamp", "app_name"],
    "sensor_below_min": ["location", "quantity", "value", "unit", "limit", "timestamp", "app_name"],
    "sensor_back_in_range": ["location", "quantity", "value", "unit", "timestamp", "app_name"],
    "sensor_silent": ["location", "sensor", "minutes", "timestamp", "app_name"],
    "sensor_speaking_again": ["location", "sensor", "timestamp", "app_name"],
    "ha_sensor_alert": ["printer", "sensor", "state", "timestamp", "app_name"],
    "location_ha_sensor_alert": ["location", "sensor", "state", "timestamp", "app_name"],
    "plug_meter_stalled": ["plug", "since", "timestamp", "app_name"],
    "test": ["app_name", "timestamp"],
    # Queue notifications
    "queue_job_added": ["job_name", "target", "timestamp", "app_name"],
    "queue_job_started": ["printer", "job_name", "estimated_time", "eta", "timestamp", "app_name"],
    "queue_job_waiting": ["job_name", "target_model", "waiting_reason", "timestamp", "app_name"],
    "queue_job_skipped": ["printer", "job_name", "reason", "timestamp", "app_name"],
    "queue_job_failed": ["printer", "job_name", "reason", "timestamp", "app_name"],
    "queue_completed": ["completed_count", "timestamp", "app_name"],
    "printer_queue_completed": ["printer", "timestamp", "app_name"],
    # User management notifications
    # Credentials/links are server-owned blocks, not editable placeholders.
    "user_created": ["username", "app_name", "timestamp"],
    "password_reset": ["username", "app_name", "timestamp"],
    "password_reset_link": ["username", "app_name", "ttl_hours", "timestamp"],
    # User email print notifications
    "user_print_start": ["username", "printer", "filename", "timestamp", "app_name"],
    "user_print_complete": ["username", "printer", "filename", "finish_photo_url", "timestamp", "app_name"],
    "user_print_failed": ["username", "printer", "filename", "finish_photo_url", "timestamp", "app_name"],
    "user_print_stopped": ["username", "printer", "filename", "finish_photo_url", "timestamp", "app_name"],
}

# Sample data for previewing templates
SAMPLE_DATA: dict[str, dict[str, str]] = {
    "plate_not_empty": {
        "printer": "Bambu X1C",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "filament_deficit": {
        "printer": "Bambu X1C",
        "print_name": "benchy.3mf",
        "needed": "200",
        "slot": "AMS 1 · slot 2",
        "available": "120",
        "missing": "80",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "ams_drying_suspended": {
        "printer": "Bambu X1C",
        "ams_label": "AMS 1",
        "cycles": "3",
        "humidity": "62",
        "threshold": "40",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "ams_drying_not_heating": {
        "printer": "Bambu P2S",
        "ams_label": "HT-A",
        "cycles": "2",
        "humidity": "75",
        "threshold": "60",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "scheduled_drying_started": {
        "printer": "Bambu X1C",
        "ams_label": "AMS 1",
        "filament": "PLA Basic Black",
        "temp": "55",
        "hours": "8",
        "schedule": "Night drying",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "scheduled_drying_completed": {
        "printer": "Bambu X1C",
        "ams_label": "AMS 1",
        "hours": "8",
        "temp": "55",
        "schedule": "Night drying",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "scheduled_drying_failed": {
        "printer": "Bambu X1C",
        "ams_label": "AMS 1",
        "reason": "The printer started a print",
        "schedule": "Night drying",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "first_layer_complete": {
        "printer": "Bambu X1C",
        "filename": "benchy.3mf",
        "total_layers": "120",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "stock_reorder_alert": {
        "filament": "PLA Basic Black",
        "brand": "Bambu Lab",
        "stock_g": "850",
        "rate_g_day": "140",
        "days_left": "6",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "stock_break_alert": {
        "filament": "PLA Basic Black",
        "brand": "Bambu Lab",
        "stock_g": "850",
        "rate_g_day": "140",
        "lead_time_days": "10",
        "days_left": "6",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "external_app_message": {
        "title": "Power lost",
        "message": "The farm is on battery power.",
        "level": "error",
        "level_icon": "🚨",
        "link": "https://homeassistant.local/lovelace/power",
        "sender": "Home Assistant · admin",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "stock_depleted_alert": {
        "filament": "PETG Basic Olive Green",
        "brand": "Sunlu",
        "rate_g_day": "140",
        "lead_time_days": "10",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "sensor_above_max": {
        "location": "Workshop",
        "quantity": "Temperature",
        "value": "34.5",
        "unit": "°C",
        "limit": "30",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "sensor_below_min": {
        "location": "Workshop",
        "quantity": "Temperature",
        "value": "34.5",
        "unit": "°C",
        "limit": "30",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "sensor_back_in_range": {
        "location": "Workshop",
        "quantity": "Temperature",
        "value": "34.5",
        "unit": "°C",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "sensor_silent": {
        "location": "Workshop",
        "sensor": "Door sensor",
        "minutes": "45",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "sensor_speaking_again": {
        "location": "Workshop",
        "sensor": "Door sensor",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "ha_sensor_alert": {
        "printer": "Bambu X1C",
        "sensor": "Door sensor",
        "state": "open",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "location_ha_sensor_alert": {
        "location": "Workshop",
        "sensor": "Door sensor",
        "state": "open",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "plug_meter_stalled": {
        "plug": "Bench plug",
        "since": "2024-01-15 12:00",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "print_start": {
        "printer": "Bambu X1C",
        "filename": "Benchy.3mf",
        "estimated_time": "1h 23m",
        "eta": "15:53",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "print_complete": {
        "printer": "Bambu X1C",
        "filename": "Benchy.3mf",
        "duration": "1h 18m",
        "filament_grams": "15.2",
        "filament_details": "AMS-A T1 PLA: 12.4g | AMS-A T3 PETG: 2.8g",
        "finish_photo_url": "/api/v1/archives/123/photos/finish_20240115_154800_abc12345.jpg",
        "timestamp": "2024-01-15 15:48",
        "app_name": "BamDude",
    },
    "print_failed": {
        "printer": "Bambu X1C",
        "filename": "Benchy.3mf",
        "duration": "0h 45m",
        "filament_grams": "7.6",
        "filament_details": "AMS-A T1 PLA: 7.6g",
        "progress": "50",
        "reason": "Filament runout",
        "finish_photo_url": "/api/v1/archives/123/photos/finish_20240115_151500_def67890.jpg",
        "timestamp": "2024-01-15 15:15",
        "app_name": "BamDude",
    },
    "print_stopped": {
        "printer": "Bambu X1C",
        "filename": "Benchy.3mf",
        "duration": "0h 30m",
        "filament_grams": "4.6",
        "filament_details": "AMS-A T2 PLA: 4.6g",
        "progress": "30",
        "finish_photo_url": "/api/v1/archives/123/photos/finish_20240115_150000_ghi11223.jpg",
        "timestamp": "2024-01-15 15:00",
        "app_name": "BamDude",
    },
    "print_progress": {
        "printer": "Bambu X1C",
        "filename": "Benchy.3mf",
        "progress": "50",
        "remaining_time": "0h 41m",
        "eta": "15:41",
        "timestamp": "2024-01-15 15:00",
        "app_name": "BamDude",
    },
    "print_paused": {
        "printer": "Bambu X1C",
        "filename": "Benchy.3mf",
        "reason": "The door seems to be open, so printing was paused.",
        "reason_code": "door_open",
        "hms_code": "0300_800F",
        "timestamp": "2024-01-15 15:10",
        "app_name": "BamDude",
    },
    "print_resumed": {
        "printer": "Bambu X1C",
        "filename": "Benchy.3mf",
        "paused_for": "0h 14m",
        "timestamp": "2024-01-15 15:24",
        "app_name": "BamDude",
    },
    "print_missing_spool_assignment": {
        "printer": "Bambu X1C",
        "missing_slots": "A1, A3",
        "missing_slot_details": "- A1: PLA Basic\n- A3: PETG HF",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "print_usage_not_recorded": {
        "printer": "Bambu X1C",
        "missing_slots": "A2",
        "missing_slot_details": "- A2: 65.5 g PLA Basic",
        "timestamp": "2024-01-15 16:10",
        "app_name": "BamDude",
    },
    "print_usage_spool_changed": {
        "printer": "Bambu X1C",
        "missing_slots": "A2",
        "missing_slot_details": "- A2: 65.5 g, Spoolman #12",
        "timestamp": "2024-01-15 16:10",
        "app_name": "BamDude",
    },
    "printer_offline": {
        "printer": "Bambu X1C",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "printer_error": {
        "printer": "Bambu X1C",
        "error_type": "AMS Error",
        "error_detail": "Filament slot 1 jammed",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "ai_failure_detection": {
        "printer": "Bambu X1C",
        "task_name": "Benchy.3mf",
        "provider": "OctoEverywhere",
        "confidence": "N/A",
        "print_quality": "2/10",
        "action": "pause",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "filament_low": {
        "printer": "Bambu X1C",
        "slot": "1",
        "remaining_percent": "15",
        "color": "Black PLA",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "maintenance_due": {
        "printer": "Bambu X1C",
        "items": "• Nozzle cleaning (OVERDUE)\n• Carbon rod lubrication (Soon)",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "ams_humidity_high": {
        "printer": "Bambu X1C",
        "ams_label": "AMS-A",
        "humidity": "75",
        "threshold": "60",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "ams_temperature_high": {
        "printer": "Bambu X1C",
        "ams_label": "AMS-A",
        "temperature": "42",
        "threshold": "35",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "bed_cooled": {
        "printer": "Bambu X1C",
        "bed_temp": "34",
        "threshold": "35",
        "filename": "Benchy",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "pause_coming_up": {
        "printer": "Bambu X1C",
        "filename": "Benchy",
        "layer": "42",
        "total_layers": "250",
        "time_to_pause": "in about 5 min",
        "minutes": "5",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "print_outcome_unchecked": {
        "printer": "Bambu X1C",
        "filename": "Benchy",
        "finished_at": "2024-01-15 13:30",
        "archive_url": "https://bamdude.example/archives?outcome=unchecked",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "test": {
        "app_name": "BamDude",
        "timestamp": "2024-01-15 14:30",
    },
    # Queue notifications
    "queue_job_added": {
        "job_name": "Benchy.3mf",
        "target": "Bambu X1C",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "queue_job_started": {
        "printer": "Bambu X1C",
        "job_name": "Benchy.3mf",
        "estimated_time": "1h 23m",
        "eta": "15:53",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "queue_job_waiting": {
        "job_name": "Benchy.3mf",
        "target_model": "X1C",
        "waiting_reason": "Printer1 (needs PLA)",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "queue_job_skipped": {
        "printer": "Bambu X1C",
        "job_name": "Benchy.3mf",
        "reason": "Previous print failed",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "queue_job_failed": {
        "printer": "Bambu X1C",
        "job_name": "Benchy.3mf",
        "reason": "Upload failed: connection timeout",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "queue_completed": {
        "completed_count": "5",
        "timestamp": "2024-01-15 18:30",
        "app_name": "BamDude",
    },
    "printer_queue_completed": {
        "printer": "Bambu X1C",
        "timestamp": "2024-01-15 18:30",
        "app_name": "BamDude",
    },
    # User management notifications.
    # Preview contains public copy only; credentials and links are appended
    # by the authentication mail service, never by an editable template.
    "user_created": {
        "username": "john_doe",
        "app_name": "BamDude",
        "timestamp": "2024-01-15 14:30",
    },
    "password_reset": {
        "username": "john_doe",
        "app_name": "BamDude",
        "timestamp": "2024-01-15 14:30",
    },
    "password_reset_link": {
        "username": "john_doe",
        "app_name": "BamDude",
        "ttl_hours": "1",
        "timestamp": "2024-01-15 14:30",
    },
    # User email print notifications
    "user_print_start": {
        "username": "john_doe",
        "printer": "Bambu X1C",
        "filename": "Benchy.3mf",
        "timestamp": "2024-01-15 14:30",
        "app_name": "BamDude",
    },
    "user_print_complete": {
        "username": "john_doe",
        "printer": "Bambu X1C",
        "filename": "Benchy.3mf",
        "timestamp": "2024-01-15 15:48",
        "app_name": "BamDude",
        "finish_photo_url": "https://bamdude.example/api/v1/notifications/photos/sample.jpg",
    },
    "user_print_failed": {
        "username": "john_doe",
        "printer": "Bambu X1C",
        "filename": "Benchy.3mf",
        "timestamp": "2024-01-15 15:15",
        "app_name": "BamDude",
        "finish_photo_url": "https://bamdude.example/api/v1/notifications/photos/sample.jpg",
    },
    "user_print_stopped": {
        "username": "john_doe",
        "printer": "Bambu X1C",
        "filename": "Benchy.3mf",
        "timestamp": "2024-01-15 15:15",
        "app_name": "BamDude",
        "finish_photo_url": "https://bamdude.example/api/v1/notifications/photos/sample.jpg",
    },
}


class NotificationTemplateBase(BaseModel):
    """Base schema for notification templates."""

    title_template: str = Field(..., min_length=1, max_length=200)
    body_template: str = Field(..., min_length=1, max_length=2000)


class NotificationTemplateUpdate(IgnoresNullForRequiredColumns):
    """Schema for updating a notification template."""

    __orm_table__ = "notification_templates"

    title_template: str | None = Field(default=None, min_length=1, max_length=200)
    body_template: str | None = Field(default=None, min_length=1, max_length=2000)


class NotificationTemplateResponse(NotificationTemplateBase):
    """Schema for notification template API responses."""

    id: int
    event_type: str
    name: str
    is_default: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EventVariablesResponse(BaseModel):
    """Response for available variables per event type."""

    event_type: str
    event_name: str
    variables: list[str]


class TemplatePreviewRequest(BaseModel):
    """Request to preview a template with sample data."""

    event_type: str
    title_template: str
    body_template: str


class TemplatePreviewResponse(BaseModel):
    """Response with rendered template preview."""

    title: str
    body: str
