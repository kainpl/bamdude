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

from pydantic import BaseModel, Field, PlainSerializer, field_validator, model_validator

from backend.app.schemas.calibration_mode import CalibrationMode
from backend.app.schemas.filament_routing import FilamentOverride
from backend.app.schemas.print_queue import serialize_utc_datetime
from backend.app.schemas.printer_location import PrinterLocationOut, reject_legacy_key
from backend.app.schemas.timelapse import TimelapseStorage
from backend.app.services.queue_source_descriptor import SourceStorageState

UTCDatetime = Annotated[datetime | None, PlainSerializer(serialize_utc_datetime)]


class AutoQueueItemCreate(BaseModel):
    # Source file (either archive_id OR library_file_id)
    archive_id: int | None = None
    library_file_id: int | None = None
    project_id: int | None = None
    # The order line this print is for; travels queue → dispatcher → archive.
    project_line_id: int | None = None

    # Routing target
    target_model: str | None = None  # auto-detected from 3MF if omitted
    target_location_id: int | None = None
    required_filament_types: list[str] | None = None  # auto-extracted from 3MF if omitted
    filament_overrides: list[FilamentOverride] | None = None
    force_color_match: bool = False

    # Multi-plate: pass a list of plate IDs to fan out N rows (one per plate).
    # Single plate_id also accepted for parity with print_queue API.
    plate_id: int | None = Field(default=None, ge=0)
    plate_ids: list[Annotated[int, Field(ge=0)]] | None = Field(default=None, max_length=64)
    # How many runs of a GIVEN plate, keyed by plate id. One shared Quantity
    # cannot say "plate 1 once, plate 2 twice" (upstream #342), and a
    # multi-plate file is exactly where that comes up. Absent — or absent for a
    # particular plate — falls back to ``quantity``, so every existing caller
    # keeps its meaning.
    plate_quantities: dict[int, int] | None = None

    # Print options (copied to print_queue on assignment). Tri-state accepted
    # (off/auto/on, or legacy bool), but auto-queue has no *_mode column, so
    # 'auto' degrades to its bool mirror on assignment — auto only survives the
    # primary PrintModal queue path (SAFE spec §2.1/§3.5).
    bed_levelling: CalibrationMode = "on"
    flow_cali: CalibrationMode = "on"
    layer_inspect: bool = False
    timelapse: bool = False
    # Which medium records it — copied onto the per-printer item at promotion.
    timelapse_storage: TimelapseStorage | None = None
    feed_policy: Literal["auto", "ams_only", "external_only"] | None = None
    use_ams: bool = True
    mesh_mode_fast_check: bool = True
    execute_swap_macros: bool = True
    swap_macro_events: list[str] | None = None
    selected_macro_ids: list[int] | None = None

    # Scheduling
    scheduled_time: datetime | None = None
    manual_start: bool = False
    auto_off_after: bool = False
    require_previous_success: bool = False

    # Batch: create N copies sharing a batch_id (1..999), like print_queue
    quantity: int = Field(default=1, ge=1, le=999)

    @model_validator(mode="before")
    @classmethod
    def _no_legacy_location(cls, values):
        return reject_legacy_key(values, "target_location", "target_location_id")

    @field_validator("plate_quantities")
    @classmethod
    def _plate_quantities_in_range(cls, value: dict[int, int] | None) -> dict[int, int] | None:
        """Same 1..999 bound ``quantity`` carries.

        Bounded on the field rather than clamped in the route: a caller far
        past the bound has made a mistake, and silently clamping hides it.
        """
        if value is None:
            return None
        for plate_id, count in value.items():
            if count < 1 or count > 999:
                raise ValueError(f"plate {plate_id}: quantity must be between 1 and 999")
        return value


class AutoQueueItemUpdate(BaseModel):
    """Editable fields for items still in status='pending'.

    Once assigned, the per-printer item is the source of truth and is
    edited via the existing ``PATCH /queue/{id}`` endpoint.
    """

    position: int | None = None
    target_model: str | None = None
    target_location_id: int | None = None
    required_filament_types: list[str] | None = None
    filament_overrides: list[FilamentOverride] | None = None
    force_color_match: bool | None = None
    scheduled_time: datetime | None = None
    manual_start: bool | None = None
    auto_off_after: bool | None = None
    require_previous_success: bool | None = None
    bed_levelling: CalibrationMode | None = None
    flow_cali: CalibrationMode | None = None
    layer_inspect: bool | None = None
    timelapse: bool | None = None
    timelapse_storage: TimelapseStorage | None = None
    feed_policy: Literal["auto", "ams_only", "external_only"] | None = None
    use_ams: bool | None = None
    mesh_mode_fast_check: bool | None = None
    execute_swap_macros: bool | None = None
    swap_macro_events: list[str] | None = None
    selected_macro_ids: list[int] | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_legacy_location(cls, values):
        return reject_legacy_key(values, "target_location", "target_location_id")


class AutoQueueItemResponse(BaseModel):
    id: int
    archive_id: int | None
    library_file_id: int | None
    project_id: int | None
    project_line_id: int | None = None

    target_model: str | None
    target_location_id: int | None = None
    target_location: PrinterLocationOut | None = None
    required_filament_types: list[str] | None = None
    filament_overrides: list[FilamentOverride] | None = None
    force_color_match: bool

    plate_id: int | None
    position: int
    scheduled_time: UTCDatetime
    manual_start: bool
    auto_off_after: bool
    require_previous_success: bool

    bed_levelling: CalibrationMode
    flow_cali: CalibrationMode
    layer_inspect: bool
    timelapse: bool
    timelapse_storage: TimelapseStorage | None = None
    feed_policy: Literal["auto", "ams_only", "external_only"] = "auto"
    use_ams: bool
    mesh_mode_fast_check: bool
    execute_swap_macros: bool
    swap_macro_events: list[str] | None = None
    selected_macro_ids: list[int] | None = None

    status: Literal["pending", "assigned", "cancelled", "failed"]
    waiting_reason: str | None
    assigned_to_item_id: int | None
    assigned_at: UTCDatetime
    cancelled_at: UTCDatetime

    print_time_seconds: int | None
    been_jumped: bool

    batch_id: str | None
    # Whether this row owns a local copy of the bytes its work prints (m173,
    # spec §8). Add-only and read-only; no auto row is ``exempt`` — the external
    # and calibration exceptions only ever reach a per-printer row. The raw spool
    # path is deliberately not exposed (§10).
    source_storage: SourceStorageState = "legacy"
    source_size_bytes: int | None = None
    # m171: set when the rebalancer moved this row's work here from another model.
    rebalanced_at: UTCDatetime = None
    rebalanced_from_model: str | None = None
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


class AutoQueueRebalanceRequest(BaseModel):
    """The rows the operator pointed at on the panel — one item, or a collapsed ``×N`` block."""

    item_ids: list[int] = Field(min_length=1, max_length=64)


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
