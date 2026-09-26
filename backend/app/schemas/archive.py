from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.schemas.calibration_mode import CalibrationMode
from backend.app.schemas.filament_routing import FilamentRoutingChoices
from backend.app.schemas.timelapse import TimelapseStorage


class ArchivePartRow(BaseModel):
    """One canonical part on the printed plate (m158)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    name_key: str
    quantity: int
    defective: int

    @classmethod
    def from_row(cls, row: object) -> "ArchivePartRow":
        """The wire shape of one ``print_archive_parts`` row.

        Named rather than spelled out per call site: three surfaces render the
        same five fields, and a field added to the row has to reach all of them
        or one screen silently stops showing it.
        """
        return cls.model_validate(row)


class ArchivePartDefective(BaseModel):
    """Per-part defect write: row id + new absolute value (not a delta)."""

    id: int
    defective: int = Field(ge=0)


class ArchiveBase(BaseModel):
    print_name: str | None = None
    is_favorite: bool | None = None
    tags: str | None = None
    notes: str | None = None
    cost: float | None = None
    failure_reason: str | None = None
    # Verbose diagnostic text for failures (HMS code / what happened) — the
    # editable twin of the short ``failure_reason`` cause code.
    error_message: str | None = None
    quantity: int | None = None  # Number of items printed
    defective_count: int | None = None  # How many of them were scrap
    # User-defined link (Printables, Thingiverse, etc.)
    external_url: str | None = None


class ArchiveUpdate(ArchiveBase):
    # Bounded where it is written, not on the base the responses share: it feeds
    # order totals, and a negative would subtract from them (upstream 30e530a8).
    # 0 is legal; a ruined plate is still recorded as defects here.
    quantity: int | None = Field(None, ge=0, le=10_000)
    printer_id: int | None = None
    project_id: int | None = None
    # The order line this print is for; validated against ``project_id``.
    project_line_id: int | None = None
    # Allow changing status (e.g., clearing failed flag)
    status: str | None = None
    # Per-part defect write (m158). Not a column on PrintArchive — the route
    # applies it to PrintArchivePart rows and derives defective_count from
    # them; it must never reach the generic setattr loop.
    parts_defective: list[ArchivePartDefective] | None = None
    # Typed by hand, above all for a print whose 3MF never arrived: nothing
    # else can supply the figure afterwards — a rescan needs the file (audit D6
    # part 2, upstream d227d422). The ARCHIVE's figure only: statistics, cost
    # and order metrics read it, and no spool is ever debited from it. Bounded
    # because it feeds those totals: a negative would subtract, and 100 kg is
    # far past any single print.
    filament_used_grams: float | None = Field(None, ge=0, le=100_000)


class ArchiveDuplicate(BaseModel):
    """Reference to a duplicate archive."""

    id: int
    print_name: str | None
    created_at: datetime | None
    match_type: str  # "exact" (hash match) or "similar" (name match)


class ArchiveResponse(BaseModel):
    id: int
    printer_id: int | None
    project_id: int | None = None
    project_line_id: int | None = None
    project_name: str | None = None  # Included for convenience
    # The library file this print was dispatched from (m014). The archive UI
    # links a print card back to the file's print history via ?file=<id>.
    library_file_id: int | None = None
    filename: str
    file_path: str
    file_size: int
    content_hash: str | None
    # Hash of the UNPATCHED source (library file or prior archive) for
    # BamDude-dispatched prints; NULL for external prints.
    source_content_hash: str | None = None
    # JSON array of patch identifiers applied before upload (v1: informational).
    applied_patches: list[str] | None = None
    # Effective hash for dedup queries: source_content_hash or content_hash.
    # Computed at response time in the route; frontend groups by this.
    effective_hash: str | None = None
    thumbnail_path: str | None
    timelapse_path: str | None
    source_3mf_path: str | None = None  # Original project 3MF from slicer
    f3d_path: str | None = None  # Fusion 360 design file

    # Duplicate detection
    duplicates: list[ArchiveDuplicate] | None = None
    duplicate_count: int = 0  # Quick count for list views
    duplicate_sequence: int = 0  # 0 = original, 1+ = nth duplicate
    original_archive_id: int | None = None  # ID of the first/original archive

    # Object count, from ``extra_data.printable_objects``. None means "no object
    # metadata", which is not the same as zero objects.
    object_count: int | None = None

    # gcode_label_objects AND exclude_object — badge in the archive list, and
    # the preview banner explains what it means. Denormalised column (m114).
    # ⚠️ Both fields are filled by ``archives.archive_to_response``, which
    # answers with a dict — ``from_attributes`` never sees the model, so a field
    # that builder does not set is this default on every response, for ever.
    skip_objects_supported: bool = False

    print_name: str | None
    print_time_seconds: int | None  # Estimated time from slicer
    actual_time_seconds: int | None = None  # Computed from started_at/completed_at
    # Percentage: 100 = perfect, >100 = faster than estimated
    time_accuracy: float | None = None
    filament_used_grams: float | None
    filament_type: str | None
    filament_color: str | None
    layer_height: float | None
    total_layers: int | None = None
    nozzle_diameter: float | None
    bed_temperature: int | None
    bed_type: str | None = None  # e.g. "Cool Plate", "Textured PEI Plate" (from 3MF curr_bed_type)
    nozzle_temperature: int | None

    sliced_for_model: str | None = None  # Printer model this file was sliced for

    # Which plate of the source 3MF was actually sent to the printer
    # (m038). Frontend uses this to drive per-plate previews and to label
    # the archive with its real plate number rather than guessing from
    # the print_name suffix.
    plate_index: int | None = None

    status: str
    started_at: datetime | None
    completed_at: datetime | None

    extra_data: dict | None

    makerworld_url: str | None
    designer: str | None
    # User-defined link (Printables, Thingiverse, etc.)
    external_url: str | None = None

    is_favorite: bool
    tags: str | None
    notes: str | None
    cost: float | None
    photos: list | None
    failure_reason: str | None
    quantity: int = 1  # Number of items printed
    # Scrap out of that plate. Shown beside ``object_count`` in the archive card
    # and list; never subtracted from any total (see models/archive.py).
    defective_count: int = 0
    # Per-part rows (m158). DETAIL responses only — list_archives never loads
    # them, to avoid an N+1 per page of archives.
    parts: list[ArchivePartRow] = []

    # Energy tracking
    energy_kwh: float | None = None
    energy_cost: float | None = None

    # Swap mode
    swap_compatible: bool = False
    # True iff the archive's 3MF has 2+ plates (extracted at archive_print()
    # / m023 backfill). Frontend uses this to gate gallery rendering.
    is_multi_plate: bool = False

    # Queue attribution (m019). For archives dispatched from a queue item or
    # batch, these tie the archive back to its origin row. External /
    # direct-dispatch archives still get ``queue_id`` (the printer's default
    # queue) but no ``batch_id``.
    queue_id: int | None = None
    batch_id: str | None = None
    # True when the AutoQueueScheduler dispatched this print. The source
    # auto_queue_items row is gone once the print finished, so this flag
    # is what the auto-queue stats view counts on.
    from_auto_queue: bool = False
    # Verbose diagnostic text for failures — the "hover to see why" twin of
    # ``failure_reason`` (short cause code).
    error_message: str | None = None

    created_at: datetime | None

    # User tracking (Issue #206)
    created_by_id: int | None = None
    created_by_username: str | None = None

    @model_validator(mode="after")
    def compute_object_count(self) -> "ArchiveResponse":
        """Compute object_count + is_multi_plate from extra_data when not set.

        Both fields live in ``extra_data`` JSON (populated at archive_print
        / m023 backfill); these defaults make the API response usable
        without forcing every reader to dig into the JSON itself.
        """
        if self.object_count is None and self.extra_data:
            printable_objects = self.extra_data.get("printable_objects")
            if printable_objects and isinstance(printable_objects, dict):
                self.object_count = len(printable_objects)
        if not self.is_multi_plate and self.extra_data:
            self.is_multi_plate = bool(self.extra_data.get("is_multi_plate"))
        return self

    class Config:
        from_attributes = True


class PaginationMeta(BaseModel):
    """Pagination metadata returned alongside paginated results."""

    total: int
    current_page: int
    per_page: int
    last_page: int


class PaginatedArchiveResponse(BaseModel):
    """Paginated archive listing with metadata."""

    data: list[ArchiveResponse]
    meta: PaginationMeta


class ArchiveFilterOptions(BaseModel):
    """Available filter values for archive dropdowns."""

    materials: list[str]
    colors: list[str]
    tags: list[str]


class ProjectPageImage(BaseModel):
    """Image embedded in 3MF project page."""

    name: str
    path: str  # Path within 3MF
    url: str  # API URL to fetch image


class ProjectPageResponse(BaseModel):
    """Project page data extracted from 3MF file."""

    # Model info
    title: str | None = None
    description: str | None = None  # HTML content
    designer: str | None = None
    designer_user_id: str | None = None
    license: str | None = None
    copyright: str | None = None
    creation_date: str | None = None
    modification_date: str | None = None
    origin: str | None = None  # "original" or "remix"

    # Profile info
    profile_title: str | None = None
    profile_description: str | None = None
    profile_cover: str | None = None
    profile_user_id: str | None = None
    profile_user_name: str | None = None

    # MakerWorld info
    design_model_id: str | None = None
    design_profile_id: str | None = None
    design_region: str | None = None

    # Images
    model_pictures: list[ProjectPageImage] = []
    profile_pictures: list[ProjectPageImage] = []
    thumbnails: list[ProjectPageImage] = []


class ReprintRequest(FilamentRoutingChoices):
    """Request body for reprinting an archive."""

    # Plate selection for multi-plate 3MF files
    # If not specified, auto-detects from file (legacy behavior for single-plate files)
    plate_id: int | None = None
    plate_name: str | None = None

    # AMS slot mapping: list of tray IDs for each filament slot in the 3MF
    # Global tray ID = (ams_id * 4) + slot_id, external = 254
    ams_mapping: list[int] | None = None

    # Print options — tri-state calibration (off/auto/on) or legacy bool.
    bed_levelling: CalibrationMode = "on"
    flow_cali: CalibrationMode = "off"
    layer_inspect: bool = False
    timelapse: bool = False
    # Which medium records it — only offered when the machine has both.
    timelapse_storage: TimelapseStorage | None = None
    use_ams: bool = True  # Not exposed in UI, but needed for API
    nozzle_offset_cali: CalibrationMode = "on"  # Dual-nozzle printers only — MQTT-gated (#1682)
    mesh_mode_fast_check: bool = True
    # Opt this reprint into per-model auto-print G-code injection (#1516). When
    # on with quantity > 1, ALL copies queue so every one is injected by the
    # scheduler (the direct first-copy path bypasses injection). No-op unless
    # snippets exist for the target model.
    gcode_injection: bool = False
    execute_swap_macros: bool = True
    swap_macro_events: list[str] | None = None
    selected_macro_ids: list[int] | None = None
    # Batch: first copy dispatches now, remaining (quantity-1) queue up
    quantity: int = 1
