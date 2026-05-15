"""Pydantic schemas for the auto-queue layer.

See ``backend/app/models/auto_queue.py`` for the ORM model and
``temp/auto-queue-adaptation-variants.md`` §12 for the full design.

The auto-queue is a *router* that sits above per-printer queues:
items here describe routing requirements (target_model, location,
filament types) without being bound to a specific printer. The
AutoQueueScheduler later assigns each item to an eligible idle printer
by *copying* it into that printer's print_queue.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PlainSerializer

from backend.app.schemas.print_queue import serialize_utc_datetime

UTCDatetime = Annotated[datetime | None, PlainSerializer(serialize_utc_datetime)]


class FilamentOverride(BaseModel):
    """Override for a single filament slot. Mirrors upstream's filament_overrides format."""

    slot_id: int = Field(ge=1)  # 1-indexed slot
    type: str | None = None  # e.g. "PLA", "PETG"
    color: str | None = None  # hex like "#FF0000"
    force_color_match: bool = False  # exact-color requirement


class AutoQueueItemCreate(BaseModel):
    # Source file (either archive_id OR library_file_id)
    archive_id: int | None = None
    library_file_id: int | None = None
    project_id: int | None = None

    # Routing target
    target_model: str | None = None  # auto-detected from 3MF if omitted
    target_location: str | None = None
    required_filament_types: list[str] | None = None  # auto-extracted from 3MF if omitted
    filament_overrides: list[FilamentOverride] | None = None
    force_color_match: bool = False

    # Multi-plate: pass a list of plate IDs to fan out N rows (one per plate).
    # Single plate_id also accepted for parity with print_queue API.
    plate_id: int | None = None
    plate_ids: list[int] | None = None

    # Print options (copied to print_queue on assignment)
    bed_levelling: bool = True
    flow_cali: bool = True
    layer_inspect: bool = False
    timelapse: bool = False
    use_ams: bool = True
    mesh_mode_fast_check: bool = True
    execute_swap_macros: bool = True
    swap_macro_events: list[str] | None = None

    # Scheduling
    scheduled_time: datetime | None = None
    manual_start: bool = False
    auto_off_after: bool = False
    require_previous_success: bool = False

    # Batch: create N copies sharing a batch_id (1..50), like print_queue
    quantity: int = Field(default=1, ge=1, le=50)


class AutoQueueItemUpdate(BaseModel):
    """Editable fields for items still in status='pending'.

    Once assigned, the per-printer item is the source of truth and is
    edited via the existing ``PATCH /queue/{id}`` endpoint.
    """

    position: int | None = None
    target_model: str | None = None
    target_location: str | None = None
    required_filament_types: list[str] | None = None
    filament_overrides: list[FilamentOverride] | None = None
    force_color_match: bool | None = None
    scheduled_time: datetime | None = None
    manual_start: bool | None = None
    auto_off_after: bool | None = None
    require_previous_success: bool | None = None
    bed_levelling: bool | None = None
    flow_cali: bool | None = None
    layer_inspect: bool | None = None
    timelapse: bool | None = None
    use_ams: bool | None = None
    mesh_mode_fast_check: bool | None = None
    execute_swap_macros: bool | None = None
    swap_macro_events: list[str] | None = None


class AutoQueueItemResponse(BaseModel):
    id: int
    archive_id: int | None
    library_file_id: int | None
    project_id: int | None

    target_model: str | None
    target_location: str | None
    required_filament_types: list[str] | None = None
    filament_overrides: list[FilamentOverride] | None = None
    force_color_match: bool

    plate_id: int | None
    position: int
    scheduled_time: UTCDatetime
    manual_start: bool
    auto_off_after: bool
    require_previous_success: bool

    bed_levelling: bool
    flow_cali: bool
    layer_inspect: bool
    timelapse: bool
    use_ams: bool
    mesh_mode_fast_check: bool
    execute_swap_macros: bool
    swap_macro_events: list[str] | None = None

    status: Literal["pending", "assigned", "cancelled"]
    waiting_reason: str | None
    assigned_to_item_id: int | None
    assigned_at: UTCDatetime
    cancelled_at: UTCDatetime

    print_time_seconds: int | None
    been_jumped: bool

    batch_id: str | None
    created_at: UTCDatetime
    created_by_id: int | None

    # UI-friendly nested data
    archive_name: str | None = None
    archive_thumbnail: str | None = None
    library_file_name: str | None = None
    library_file_thumbnail: str | None = None
    created_by_username: str | None = None
    # When assigned, surface the printer for UI link
    assigned_printer_id: int | None = None
    assigned_printer_name: str | None = None

    class Config:
        from_attributes = True


class AutoQueueReorderItem(BaseModel):
    id: int
    position: int


class AutoQueueReorder(BaseModel):
    items: list[AutoQueueReorderItem]


class AutoQueueBatchActionResponse(BaseModel):
    """Result of batch cancel/skip/reorder operations."""

    affected: int
    batch_id: str


class AutoQueueStatsResponse(BaseModel):
    """Archive-backed terminal totals for auto-queue dispatched prints.

    Mirrors the per-printer queue card footer (``get_queue_terminal_counts``)
    — counts ``print_archives`` rows flagged ``from_auto_queue``. ``cancelled``
    folds in the ``aborted`` / ``stopped`` failure family.
    """

    completed_count: int
    failed_count: int
    cancelled_count: int
    total_count: int
