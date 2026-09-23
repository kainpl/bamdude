from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PlainSerializer, model_validator

from backend.app.schemas.calibration_mode import CalibrationMode
from backend.app.schemas.filament_routing import FilamentRoutingChoices
from backend.app.schemas.timelapse import TimelapseStorage
from backend.app.services.queue_source_descriptor import SourceStorageState
from backend.app.utils.temperature_limits import MAX_CHAMBER_TEMP_C


# Custom serializer to ensure UTC datetimes have Z suffix
def serialize_utc_datetime(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat() + "Z"


UTCDatetime = Annotated[datetime | None, PlainSerializer(serialize_utc_datetime)]


class PrintQueueItemCreate(FilamentRoutingChoices):
    queue_id: int  # Required - which printer's queue to add to
    # One-time placement for this newly created block. It is intentionally not
    # stored on the row: after insertion normal queue ordering takes over.
    enqueue_position: Literal["end", "next"] = "end"
    # Exactly one source is required. ``source_queue_item_id`` reuses the
    # immutable managed bytes of an existing queued job; it is intentionally
    # not a file path and never makes the server read that job's original again.
    archive_id: int | None = None
    library_file_id: int | None = None
    source_queue_item_id: int | None = Field(default=None, gt=0)
    scheduled_time: datetime | None = None  # None = ASAP
    auto_off_after: bool = False
    manual_start: bool = False
    # Refuse to dispatch while the last finished print on this printer is a
    # failure (m116). Off by default — a gate nobody asked for is a stalled farm.
    require_previous_success: bool = False
    ams_mapping: list[int] | None = None
    plate_id: int | None = Field(default=None, ge=0)
    # Print options — bed_levelling / flow_cali / nozzle_offset_cali are
    # tri-state (off/auto/on); the CalibrationMode field also accepts a legacy
    # bool (True->'on', False->'off') so older API clients keep working.
    bed_levelling: CalibrationMode = "on"
    flow_cali: CalibrationMode = "on"
    layer_inspect: bool = False
    timelapse: bool = False
    # Which medium records it — only offered when the machine has both.
    timelapse_storage: TimelapseStorage | None = None
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
    preheat_chamber_target_override: int | None = Field(default=None, ge=0, le=MAX_CHAMBER_TEMP_C)
    # Batch: create N identical items sharing a batch_id (1..999). Copies are
    # cheap — one ORM row each, single transaction, the 3MF stored once — so
    # the bound is a fat-finger guard, not a capacity limit (was 50, an
    # arbitrary round number inherited from the upstream batch feature).
    quantity: int = Field(default=1, ge=1, le=999)
    # Project to associate the resulting archive with (when triggered from project view)
    project_id: int | None = None
    # The order line this print is for; travels queue → dispatcher → archive.
    project_line_id: int | None = None


class PrintQueueNextBatchCreate(BaseModel):
    """Several ASAP queue entries inserted as one contiguous next block."""

    items: list[PrintQueueItemCreate] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _validate_next_block(self) -> "PrintQueueNextBatchCreate":
        first = self.items[0]
        source = (first.archive_id, first.library_file_id, first.source_queue_item_id)
        if any(item.queue_id != first.queue_id for item in self.items):
            raise ValueError("All next-block items must target one queue")
        if any((item.archive_id, item.library_file_id, item.source_queue_item_id) != source for item in self.items):
            raise ValueError("All next-block items must use one source")
        if any(item.enqueue_position != "next" for item in self.items):
            raise ValueError("Next-block items must use enqueue_position=next")
        if any(item.manual_start or item.scheduled_time is not None for item in self.items):
            raise ValueError("Run next is available only for ASAP jobs")
        return self


class QueueCopySourceProfile(BaseModel):
    """Everything PrintModal needs from a queue row's immutable source.

    This intentionally carries parsed metadata, not a source path or a download
    URL.  It lets the copy UI make current AMS and print-option choices after
    the archive/library record has disappeared.
    """

    item_id: int
    filename: str
    sliced_for_model: str | None = None
    swap_compatible: bool = False
    plates: list[dict]
    is_multi_plate: bool = False


class PrintQueueItemUpdate(FilamentRoutingChoices):
    queue_id: int | None = None  # Move to different printer's queue
    position: int | None = None
    scheduled_time: datetime | None = None
    auto_off_after: bool | None = None
    manual_start: bool | None = None
    require_previous_success: bool | None = None
    ams_mapping: list[int] | None = None
    plate_id: int | None = Field(default=None, ge=0)
    # Print options — tri-state calibration (off/auto/on) or legacy bool; None
    # (field unset) means "leave unchanged".
    bed_levelling: CalibrationMode | None = None
    flow_cali: CalibrationMode | None = None
    layer_inspect: bool | None = None
    timelapse: bool | None = None
    timelapse_storage: TimelapseStorage | None = None
    use_ams: bool | None = None
    nozzle_offset_cali: CalibrationMode | None = None
    mesh_mode_fast_check: bool | None = None
    execute_swap_macros: bool | None = None
    swap_macro_events: list[str] | None = None
    selected_macro_ids: list[int] | None = None
    gcode_injection: bool | None = None
    preheat_override: Literal["inherit", "on", "off"] | None = None
    preheat_chamber_target_override: int | None = Field(default=None, ge=0, le=MAX_CHAMBER_TEMP_C)
    # H2C dual-nozzle-rack slicer pick (#1780). The slicer's per-filament
    # physical nozzle position IDs — an opaque list[int] BambuStudio sends in
    # its project_file MQTT body; replayed to the printer verbatim on dispatch.
    nozzle_mapping: list[int] | None = None


class PrintQueueItemResponse(BaseModel):
    filament_routing: dict | None = None
    id: int
    queue_id: int
    printer_id: int | None = None  # Convenience - resolved from queue
    project_id: int | None = None  # Linked project (the order this print is for)
    project_line_id: int | None = None  # Which line of that order
    # The order's name, for surfaces that show where a row is filed without
    # another round trip (the copy-queue dialog). None when the row is filed
    # under no order — or when the endpoint did not load the relationship.
    project_name: str | None = None
    waiting_reason: str | None = None
    archive_id: int | None
    library_file_id: int | None
    position: int
    # Who put the row here: "queue" (scheduled), "direct" (Print dialog),
    # "external" (BamDude never sent it). Read-only — set at creation, carried
    # by clone and repeat. See m160.
    origin: str = "queue"
    # Every row THIS call created, in creation order — a quantity becomes rows,
    # not a column, so an add of three copies made three of them and used to
    # report only the first. Copying a queue and re-forming its batches on the
    # target needs all of them.
    #
    # ⚠️ ``None``, not ``[]``, everywhere the question does not arise: this
    # schema also serialises listings, and an empty list there would read as
    # "this call created nothing" rather than "nothing was created by a call".
    created_item_ids: list[int] | None = None
    scheduled_time: UTCDatetime
    auto_off_after: bool
    manual_start: bool
    require_previous_success: bool = False
    ams_mapping: list[int] | None = None
    plate_id: int | None = Field(default=None, ge=0)
    # Print options — tri-state calibration (off/auto/on). Derived server-side
    # from the *_mode column (falling back to the legacy bool) in _enrich_response.
    bed_levelling: CalibrationMode = "on"
    flow_cali: CalibrationMode = "on"
    layer_inspect: bool = False
    timelapse: bool = False
    # Which medium records it — only offered when the machine has both.
    timelapse_storage: TimelapseStorage | None = None
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
    # Whether this job owns a local copy of the bytes it prints (m173, spec §8).
    # Add-only and read-only: ``ready`` is set for an attached, verified blob and
    # never inferred from the kind of the original source; ``exempt`` is an
    # external print or a calibration job; ``legacy`` is a row the background
    # hydration has still to reach. The raw spool path is deliberately NOT
    # exposed — there is no "print an arbitrary hash" surface (§10).
    source_storage: SourceStorageState = "legacy"
    source_size_bytes: int | None = None
    # Whether ``GET /queue/{id}/source-thumbnail`` has a picture to serve for this
    # row: the render of the job's OWN plate inside the bytes it captured (spec §4
    # — the thumbnail is recoverable from the stored 3MF, and the UI must not
    # require the original's).
    #
    # ⚠️ A **boolean**, unlike the ``*_thumbnail`` fields below, which are the
    # server's disk paths and only say that a picture exists somewhere. This one
    # answers "may I ask for it", so a row with no recoverable picture says
    # ``False`` and the UI draws its honest empty state instead of a broken image.
    # ``False`` for every legacy row, whose picture still comes from whichever
    # original row it names.
    source_thumbnail: bool = False

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


class PrintQueueBulkUpdate(FilamentRoutingChoices):
    """Bulk update multiple queue items with the same values."""

    ams_mapping: list[int] | None = None

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
    timelapse_storage: TimelapseStorage | None = None
    use_ams: bool | None = None
    nozzle_offset_cali: CalibrationMode | None = None
    mesh_mode_fast_check: bool | None = None
    execute_swap_macros: bool | None = None
    swap_macro_events: list[str] | None = None
    selected_macro_ids: list[int] | None = None
    gcode_injection: bool | None = None
    preheat_override: Literal["inherit", "on", "off"] | None = None
    preheat_chamber_target_override: int | None = Field(default=None, ge=0, le=MAX_CHAMBER_TEMP_C)


class PrintQueueBulkUpdateResponse(BaseModel):
    """Response for bulk update operation."""

    updated_count: int
    skipped_count: int
    message: str


class PrintQueueBulkDelete(BaseModel):
    """Delete the listed rows that are still failed or cancelled.

    Selection-scoped by construction: the client names the rows it showed,
    the server never derives a set from a status filter.
    """

    item_ids: list[int]


class PrintQueueBulkDeleteResponse(BaseModel):
    deleted_count: int
    skipped_count: int
    message: str
