from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PlainSerializer, model_validator

from backend.app.schemas.calibration_mode import CalibrationMode


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
    # Refuse to dispatch while the last finished print on this printer is a
    # failure (m116). Off by default — a gate nobody asked for is a stalled farm.
    require_previous_success: bool = False
    ams_mapping: list[int] | None = None
    plate_id: int | None = None
    # Print options — bed_levelling / flow_cali / nozzle_offset_cali are
    # tri-state (off/auto/on); the CalibrationMode field also accepts a legacy
    # bool (True->'on', False->'off') so older API clients keep working.
    bed_levelling: CalibrationMode = "on"
    flow_cali: CalibrationMode = "on"
    layer_inspect: bool = False
    timelapse: bool = False
    use_ams: bool = True
    # Nozzle offset calibration — dual-nozzle printers only (#1682). Default 'on'
    # matches BambuStudio; the MQTT layer forces "skip" on single-nozzle printers.
    nozzle_offset_cali: CalibrationMode = "on"
    mesh_mode_fast_check: bool = True
    execute_swap_macros: bool = True
    swap_macro_events: list[str] | None = None
    selected_macro_ids: list[int] | None = None
    gcode_injection: bool = False
    # Preheat / heat-soak per-item override (#1468). 'inherit' uses the global
    # preheat_enabled setting; 'on' / 'off' force the decision. The chamber target
    # falls through: this override → max(filament-map[loaded tray]) → 0.
    preheat_override: Literal["inherit", "on", "off"] = "inherit"
    preheat_chamber_target_override: int | None = Field(default=None, ge=0, le=60)
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
    require_previous_success: bool | None = None
    ams_mapping: list[int] | None = None
    plate_id: int | None = None
    # Print options — tri-state calibration (off/auto/on) or legacy bool; None
    # (field unset) means "leave unchanged".
    bed_levelling: CalibrationMode | None = None
    flow_cali: CalibrationMode | None = None
    layer_inspect: bool | None = None
    timelapse: bool | None = None
    use_ams: bool | None = None
    nozzle_offset_cali: CalibrationMode | None = None
    mesh_mode_fast_check: bool | None = None
    execute_swap_macros: bool | None = None
    swap_macro_events: list[str] | None = None
    selected_macro_ids: list[int] | None = None
    gcode_injection: bool | None = None
    preheat_override: Literal["inherit", "on", "off"] | None = None
    preheat_chamber_target_override: int | None = Field(default=None, ge=0, le=60)
    # H2C dual-nozzle-rack slicer pick (#1780). The slicer's per-filament
    # physical nozzle position IDs — an opaque list[int] BambuStudio sends in
    # its project_file MQTT body; replayed to the printer verbatim on dispatch.
    nozzle_mapping: list[int] | None = None


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
    require_previous_success: bool = False
    ams_mapping: list[int] | None = None
    plate_id: int | None = None
    # Print options — tri-state calibration (off/auto/on). Derived server-side
    # from the *_mode column (falling back to the legacy bool) in _enrich_response.
    bed_levelling: CalibrationMode = "on"
    flow_cali: CalibrationMode = "on"
    layer_inspect: bool = False
    timelapse: bool = False
    use_ams: bool = True
    # Nozzle offset calibration — dual-nozzle printers only (#1682). Default 'on'
    # matches BambuStudio; the MQTT layer forces "skip" on single-nozzle printers.
    nozzle_offset_cali: CalibrationMode = "on"
    mesh_mode_fast_check: bool = True
    execute_swap_macros: bool = True
    swap_macro_events: list[str] | None = None
    selected_macro_ids: list[int] | None = None
    gcode_injection: bool = False
    preheat_override: Literal["inherit", "on", "off"] = "inherit"
    preheat_chamber_target_override: int | None = None
    # H2C dual-nozzle-rack slicer pick (#1780). Surface for any future
    # "edit print → choose nozzle" UI; null on every model except O1C2
    # uploads from BambuStudio.
    nozzle_mapping: list[int] | None = None
    status: Literal["pending", "printing", "completed", "failed", "skipped", "cancelled"]
    started_at: UTCDatetime
    completed_at: UTCDatetime
    error_message: str | None
    created_at: UTCDatetime
    batch_id: str | None = None

    # Nested info for UI
    archive_name: str | None = None
    archive_thumbnail: str | None = None
    # True when the linked archive has been soft-deleted (trashed): its files
    # are gone, so the archive-derived fields above are suppressed (#1348).
    archive_deleted: bool = False
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
    # Build plate type (e.g. "Textured PEI Plate") so the user knows which plate
    # to mount on the printer (#1281). Per-plate accurate on multi-plate 3MFs:
    # when ``plate_id`` is set, the value is the matching plate's
    # ``curr_bed_type`` rather than the archive-level first-plate default.
    bed_type: str | None = None

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

    @model_validator(mode="after")
    def _validate_positions_unique(self) -> "PrintQueueReorder":
        """Reject reorder payloads with duplicate positions (upstream #1625-followup).

        /reorder is the bulk renumber path; a well-behaved client sends a
        contiguous renumbering of one queue's pending items. Two items at the
        same position would leave the queue ambiguous (the scheduler's
        ORDER BY (queue_id, position) breaks ties by physical row order). Fail
        closed at the schema boundary so the bug is caught before any DB write.
        Uniqueness is enforced within the payload only.
        """
        positions = [it.position for it in self.items]
        if len(positions) != len(set(positions)):
            duplicates = sorted({p for p in positions if positions.count(p) > 1})
            raise ValueError(f"Duplicate positions in reorder request: {duplicates}")
        return self


class PrintQueueBatchCreate(BaseModel):
    """Group existing pending queue items under a new shared batch_id."""

    item_ids: list[int]


class PrintQueueBulkUpdate(BaseModel):
    """Bulk update multiple queue items with the same values."""

    item_ids: list[int]
    queue_id: int | None = None  # Move all to different queue
    scheduled_time: datetime | None = None
    auto_off_after: bool | None = None
    manual_start: bool | None = None
    require_previous_success: bool | None = None
    # Print options — tri-state calibration (off/auto/on) or legacy bool.
    bed_levelling: CalibrationMode | None = None
    flow_cali: CalibrationMode | None = None
    layer_inspect: bool | None = None
    timelapse: bool | None = None
    use_ams: bool | None = None
    nozzle_offset_cali: CalibrationMode | None = None
    mesh_mode_fast_check: bool | None = None
    execute_swap_macros: bool | None = None
    swap_macro_events: list[str] | None = None
    selected_macro_ids: list[int] | None = None
    gcode_injection: bool | None = None
    preheat_override: Literal["inherit", "on", "off"] | None = None
    preheat_chamber_target_override: int | None = Field(default=None, ge=0, le=60)


class PrintQueueBulkUpdateResponse(BaseModel):
    """Response for bulk update operation."""

    updated_count: int
    skipped_count: int
    message: str
