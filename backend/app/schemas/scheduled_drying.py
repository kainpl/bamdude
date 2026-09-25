"""Scheduled AMS drying — one-shot runs and recurring rules (vault 60-specs/scheduled-drying-spec)."""

from datetime import datetime

from pydantic import BaseModel, Field

from backend.app.schemas.print_queue import UTCDatetime


class ScheduledDryingCreate(BaseModel):
    printer_id: int
    ams_id: int = 0
    temp: int
    duration_hours: int
    # max_length matches String(50): PostgreSQL rejects longer, SQLite silently stores it.
    filament: str = Field("", max_length=50)
    rotate_tray: bool = False
    # None = as soon as the printer is free. An offset-aware value is converted to UTC.
    start_after: datetime | None = None


class ScheduledDryingResponse(BaseModel):
    id: int
    printer_id: int
    ams_id: int
    temp: int
    duration_hours: int
    filament: str
    rotate_tray: bool
    schedule_id: int | None
    start_after: UTCDatetime
    latest_start: UTCDatetime
    status: str
    reason: str | None
    detail: str | None
    created_at: UTCDatetime
    started_at: UTCDatetime
    completed_at: UTCDatetime

    model_config = {"from_attributes": True}


class DryingScheduleCreate(BaseModel):
    printer_id: int
    ams_id: int = 0
    temp: int
    duration_hours: int
    filament: str = Field("", max_length=50)
    rotate_tray: bool = False
    # "HH:MM", server time.
    start_time: str
    weekdays: int = 127
    latest_start: str | None = None
    enabled: bool = True


class DryingScheduleUpdate(BaseModel):
    ams_id: int | None = None
    temp: int | None = None
    duration_hours: int | None = None
    filament: str | None = Field(None, max_length=50)
    rotate_tray: bool | None = None
    start_time: str | None = None
    weekdays: int | None = None
    latest_start: str | None = None
    enabled: bool | None = None


class DryingScheduleResponse(BaseModel):
    id: int
    printer_id: int
    ams_id: int
    temp: int
    duration_hours: int
    filament: str
    rotate_tray: bool
    start_time: str
    weekdays: int
    latest_start: str | None
    enabled: bool
    created_at: UTCDatetime
    updated_at: UTCDatetime

    model_config = {"from_attributes": True}


class DryingScheduleList(BaseModel):
    # IANA name of the zone rules run in — the frontend has nowhere else to learn it.
    server_timezone: str
    schedules: list[DryingScheduleResponse]
