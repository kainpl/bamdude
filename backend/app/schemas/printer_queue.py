"""Pydantic schemas for printer queue management."""

from datetime import datetime

from pydantic import BaseModel


class PrinterQueueResponse(BaseModel):
    """Response schema for a printer queue."""

    id: int
    printer_id: int
    printer_name: str | None = None
    printer_model: str | None = None
    printer_location: str | None = None
    status: str  # idle, printing, paused, error
    last_activity_at: datetime | None
    current_item_id: int | None
    pending_count: int
    completed_count: int
    failed_count: int
    cancelled_count: int
    skipped_count: int
    total_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PrinterQueueUpdate(BaseModel):
    """Update schema for a printer queue (pause/resume)."""

    status: str | None = None  # idle, paused
