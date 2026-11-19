"""Pydantic schemas for printer queue management."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.app.schemas.not_null import IgnoresNullForRequiredColumns
from backend.app.schemas.printer_location import PrinterLocationOut
from backend.app.schemas.printer_tag import PrinterTagOut
from backend.app.services.queue_status import QueueState


class QueueStatusRow(BaseModel):
    queue_id: int
    printer_id: int
    status: str
    is_paused: bool
    effective_state: QueueState


class QueueStatusSummary(BaseModel):
    queues: list[QueueStatusRow]
    attention: Literal["none", "paused", "error"]


class PrinterQueueResponse(BaseModel):
    """Response schema for a printer queue."""

    id: int
    printer_id: int
    printer_name: str | None = None
    printer_model: str | None = None
    printer_location: PrinterLocationOut | None = None
    #: The printer's tags, the shape every response embeds — the queue page
    #: sorts and groups by them exactly as the printers page does.
    printer_tags: list[PrinterTagOut] = Field(default_factory=list)
    status: str  # idle, printing, paused, error
    is_paused: bool = False  # operator-controlled queue pause, orthogonal to status
    auto_distribute_eligible: bool = True  # opt-out from AutoQueue routing
    last_activity_at: datetime | None
    current_item_id: int | None
    pending_count: int
    completed_count: int
    failed_count: int
    cancelled_count: int
    skipped_count: int
    total_count: int
    #: The newest finished print on this printer failed and nobody acknowledged it: work marked "only after a
    #: successful previous print" waits for this printer (AutoQueue routes it elsewhere or holds it). A later
    #: success, a Retry of the failure or ``POST /queues/{id}/acknowledge-failure`` clears it.
    previous_print_failed: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AcknowledgeFailureResponse(BaseModel):
    """``POST /queues/{id}/acknowledge-failure``: whether a failure was gating, and is acknowledged now."""

    acknowledged: bool


class PrinterQueueUpdate(IgnoresNullForRequiredColumns):
    """Update schema for a printer queue (pause/resume)."""

    __orm_table__ = "printer_queues"

    status: str | None = None  # idle, paused
    is_paused: bool | None = None  # operator queue pause — allowed in any status
    auto_distribute_eligible: bool | None = None  # AutoQueue routing opt-out
