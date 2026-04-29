from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PlainSerializer


# Custom serializer to ensure UTC datetimes have Z suffix
def serialize_utc_datetime(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat() + "Z"


UTCDatetime = Annotated[datetime | None, PlainSerializer(serialize_utc_datetime)]


class PrintQueueItemCreate(BaseModel):
    queue_id: int  # Required - which printer's queue to add to
    # Either archive_id OR library_file_id must be provided
    archive_id: int | None = None
    library_file_id: int | None = None
    scheduled_time: datetime | None = None  # None = ASAP
    auto_off_after: bool = False
    manual_start: bool = False
    ams_mapping: list[int] | None = None
    plate_id: int | None = None
    # Print options
    bed_levelling: bool = True
    flow_cali: bool = True
    layer_inspect: bool = False
    timelapse: bool = False
    use_ams: bool = True
    mesh_mode_fast_check: bool = True
    execute_swap_macros: bool = True
    swap_macro_events: list[str] | None = None
    gcode_injection: bool = False
    # Batch: create N identical items sharing a batch_id (1..50)
    quantity: int = Field(default=1, ge=1, le=50)
    # Project to associate the resulting archive with (when triggered from project view)
    project_id: int | None = None


class PrintQueueItemUpdate(BaseModel):
    queue_id: int | None = None  # Move to different printer's queue
    position: int | None = None
    scheduled_time: datetime | None = None
    auto_off_after: bool | None = None
    manual_start: bool | None = None
    ams_mapping: list[int] | None = None
    plate_id: int | None = None
    # Print options
    bed_levelling: bool | None = None
    flow_cali: bool | None = None
    layer_inspect: bool | None = None
    timelapse: bool | None = None
    use_ams: bool | None = None
    mesh_mode_fast_check: bool | None = None
    execute_swap_macros: bool | None = None
    swap_macro_events: list[str] | None = None
    gcode_injection: bool | None = None


class PrintQueueItemResponse(BaseModel):
    id: int
    queue_id: int
    printer_id: int | None = None  # Convenience - resolved from queue
    project_id: int | None = None  # Linked project (inherited from library_file or set directly)
    waiting_reason: str | None = None
    archive_id: int | None
    library_file_id: int | None
    position: int
    scheduled_time: UTCDatetime
    auto_off_after: bool
    manual_start: bool
    ams_mapping: list[int] | None = None
    plate_id: int | None = None
    # Print options
    bed_levelling: bool = True
    flow_cali: bool = True
    layer_inspect: bool = False
    timelapse: bool = False
    use_ams: bool = True
    mesh_mode_fast_check: bool = True
    execute_swap_macros: bool = True
    swap_macro_events: list[str] | None = None
    gcode_injection: bool = False
    status: Literal["pending", "printing", "completed", "failed", "skipped", "cancelled"]
    started_at: UTCDatetime
    completed_at: UTCDatetime
    error_message: str | None
    created_at: UTCDatetime
    batch_id: str | None = None

    # Nested info for UI
    archive_name: str | None = None
    archive_thumbnail: str | None = None
    library_file_name: str | None = None
    library_file_thumbnail: str | None = None
    printer_name: str | None = None
    print_time_seconds: int | None = None
    filament_used_grams: float | None = None
    filament_type: str | None = None
    filament_color: str | None = None
    layer_height: float | None = None
    nozzle_diameter: float | None = None
    sliced_for_model: str | None = None

    # User tracking
    created_by_id: int | None = None
    created_by_username: str | None = None

    # Virtual-item fields (set by ``build_virtual_current_print`` for
    # external / direct-dispatch prints that have no DB row).  Real
    # queue items default to False + None.
    is_virtual: bool = False
    source: str | None = None  # 'external' | 'bamdude_direct' | 'bamdude_queue' (real items)

    class Config:
        from_attributes = True


class PrintQueueReorderItem(BaseModel):
    id: int
    position: int


class PrintQueueReorder(BaseModel):
    items: list[PrintQueueReorderItem]


class PrintQueueBulkUpdate(BaseModel):
    """Bulk update multiple queue items with the same values."""

    item_ids: list[int]
    queue_id: int | None = None  # Move all to different queue
    scheduled_time: datetime | None = None
    auto_off_after: bool | None = None
    manual_start: bool | None = None
    # Print options
    bed_levelling: bool | None = None
    flow_cali: bool | None = None
    layer_inspect: bool | None = None
    timelapse: bool | None = None
    use_ams: bool | None = None
    mesh_mode_fast_check: bool | None = None
    execute_swap_macros: bool | None = None
    swap_macro_events: list[str] | None = None
    gcode_injection: bool | None = None


class PrintQueueBulkUpdateResponse(BaseModel):
    """Response for bulk update operation."""

    updated_count: int
    skipped_count: int
    message: str
