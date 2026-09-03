from datetime import datetime

from pydantic import BaseModel, Field, field_validator


def validate_http_url(value: str | None) -> str | None:
    """Reject anything that isn't an http(s) URL — the URL is rendered as a
    clickable `<a href>` so a `javascript:` / `data:` / `file:` value would
    be an XSS vector even with React's default escaping (#1155).

    Public because it guards every operator-supplied link in the domain, not
    just a project's: ``schemas/product.py`` validates ``source_url`` with it
    too. The old private name stays as an alias below for the legacy callers.
    """
    if value is None:
        return value
    trimmed = value.strip()
    if not trimmed:
        return None
    lowered = trimmed.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise ValueError("url must start with http:// or https://")
    return trimmed


_validate_project_url = validate_http_url


class ProjectCreate(BaseModel):
    """Schema for creating a new project."""

    name: str
    description: str | None = None
    color: str | None = None
    target_count: int | None = None
    target_parts_count: int | None = None
    notes: str | None = None
    tags: str | None = None
    due_date: datetime | None = None
    priority: str = "normal"
    budget: float | None = None
    parent_id: int | None = None  # For sub-projects
    url: str | None = None

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        return _validate_project_url(v)


class ProjectUpdate(BaseModel):
    """Schema for updating a project."""

    name: str | None = None
    description: str | None = None
    color: str | None = None
    status: str | None = None  # active, completed, archived
    target_count: int | None = None
    target_parts_count: int | None = None
    notes: str | None = None
    tags: str | None = None
    due_date: datetime | None = None
    priority: str | None = None
    budget: float | None = None
    parent_id: int | None = None
    url: str | None = None

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        return _validate_project_url(v)


class ProjectDuplicate(BaseModel):
    """Options for copying an existing project into a new one.

    Everything that describes *how the project is set up* is copied; nothing
    that records *what has happened to it* is. See the route for the exact
    split — it is the part users ask about.
    """

    name: str | None = None  # defaults to "<source> (Copy)", de-duplicated
    include_children: bool = False  # duplicate the whole sub-project tree


class ProjectStats(BaseModel):
    """Statistics for a project."""

    total_archives: int = 0  # Number of archive records
    total_items: int = 0  # Sum of quantities (total items printed)
    completed_prints: int = 0  # Sum of quantities for completed prints
    failed_prints: int = 0  # Sum of quantities for failed prints
    queued_prints: int = 0
    in_progress_prints: int = 0
    total_print_time_hours: float = 0.0
    total_filament_grams: float = 0.0
    # Scrap among the completed prints, already subtracted from
    # ``completed_prints``. Surfaced so the page can say why the parts
    # tally is lower than what came off the plates.
    defective_parts: int = 0
    progress_percent: float | None = None  # Based on target_count (plates)
    parts_progress_percent: float | None = None  # Based on target_parts_count
    # Cost tracking (Phase 6)
    estimated_cost: float = 0.0  # Based on filament cost
    total_energy_kwh: float = 0.0
    total_energy_cost: float = 0.0
    remaining_prints: int | None = None  # target_count - total_archives
    remaining_parts: int | None = None  # target_parts_count - completed_prints
    # BOM stats (Phase 7)
    bom_total_items: int = 0
    bom_completed_items: int = 0
    bom_cost: float = 0.0  # Total cost of BOM items (sum of unit_price * quantity_needed)


class ProjectChildPreview(BaseModel):
    """Minimal project data for child preview."""

    id: int
    name: str
    color: str | None
    status: str
    progress_percent: float | None = None


class ProjectResponse(BaseModel):
    """Schema for project response."""

    id: int
    name: str
    description: str | None
    color: str | None
    status: str
    target_count: int | None
    target_parts_count: int | None = None
    notes: str | None = None
    attachments: list | None = None
    tags: str | None = None
    due_date: datetime | None = None
    priority: str = "normal"
    budget: float | None = None
    is_template: bool = False
    template_source_id: int | None = None
    parent_id: int | None = None
    parent_name: str | None = None  # For display
    children: list[ProjectChildPreview] = []
    url: str | None = None
    cover_image_filename: str | None = None
    created_at: datetime
    updated_at: datetime
    stats: ProjectStats | None = None
    # Everything under this project, its own prints included — present only when
    # it actually has sub-projects.
    #
    # ⚠️ A SECOND figure rather than a widening of ``stats``. Nesting has been
    # settable over the API all along, so broadening the existing numbers would
    # silently restate the history of anyone who already used it; and a master
    # project still has its own prints, which are a different question from what
    # the tree did.
    rollup_stats: ProjectStats | None = None

    class Config:
        from_attributes = True


class ArchivePreview(BaseModel):
    """Minimal archive data for project preview."""

    id: int
    print_name: str | None
    thumbnail_path: str | None
    status: str
    filament_type: str | None = None
    filament_color: str | None = None


class ProjectListResponse(BaseModel):
    """Schema for project list item (lighter weight)."""

    id: int
    name: str
    description: str | None
    color: str | None
    status: str
    target_count: int | None
    target_parts_count: int | None = None
    budget: float | None = None
    created_at: datetime
    # Card-level metadata the shared edit dialog seeds itself from. The dialog
    # is handed whichever project object the caller has — a list item on the
    # Projects page, a full project on the detail page — so anything it edits
    # has to be on BOTH payloads or editing from the list silently submits the
    # dialog's defaults over stored values (upstream #2536).
    tags: str | None = None
    due_date: datetime | None = None
    priority: str = "normal"
    # Quick stats
    archive_count: int = 0  # Number of print jobs
    total_items: int = 0  # Sum of quantities (total items printed, including failed)
    completed_count: int = 0  # Sum of quantities for completed prints only
    # Scrap off completed plates. Already subtracted from ``completed_count``,
    # which is why the card needs it separately: without it the parts figure
    # silently reads lower than the plates produced, with nothing saying why.
    defective_count: int = 0  # Sum of defective_count over completed prints
    failed_count: int = 0  # Sum of quantities for failed prints
    queue_count: int = 0
    progress_percent: float | None = None
    # Preview of archives (up to 5)
    archives: list[ArchivePreview] = []
    url: str | None = None
    cover_image_filename: str | None = None
    # Nesting, so a list-only caller can group and can offer a parent picker
    # that already knows which projects would close a loop.
    parent_id: int | None = None
    is_template: bool = False

    class Config:
        from_attributes = True


class BatchAddArchives(BaseModel):
    """Schema for batch adding archives to a project."""

    archive_ids: list[int]


class BatchAddQueueItems(BaseModel):
    """Schema for batch adding queue items to a project."""

    queue_item_ids: list[int]


# Phase 7: BOM Schemas - Tracks sourced/purchased parts
class BOMItemCreate(BaseModel):
    """Schema for creating a BOM item."""

    name: str
    quantity_needed: int = 1
    unit_price: float | None = None
    sourcing_url: str | None = None
    archive_id: int | None = None
    stl_filename: str | None = None
    remarks: str | None = None


class BOMItemUpdate(BaseModel):
    """Schema for updating a BOM item."""

    name: str | None = None
    quantity_needed: int | None = None
    quantity_acquired: int | None = None
    unit_price: float | None = None
    sourcing_url: str | None = None
    archive_id: int | None = None
    stl_filename: str | None = None
    remarks: str | None = None


class BOMItemResponse(BaseModel):
    """Schema for BOM item response."""

    id: int
    project_id: int
    name: str
    quantity_needed: int
    quantity_acquired: int
    unit_price: float | None
    sourcing_url: str | None
    archive_id: int | None
    archive_name: str | None = None
    stl_filename: str | None
    remarks: str | None
    sort_order: int
    is_complete: bool = False
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Phase 9: Timeline Schemas
class TimelineEvent(BaseModel):
    """Schema for a timeline event."""

    event_type: str  # archive_added, queue_started, queue_completed, status_changed, note_updated
    timestamp: datetime
    title: str
    description: str | None = None
    metadata: dict | None = None  # Additional event-specific data


# Phase 10: Import/Export Schemas
class BOMItemExport(BaseModel):
    """Schema for exporting a BOM item."""

    name: str
    quantity_needed: int
    quantity_acquired: int
    unit_price: float | None
    sourcing_url: str | None
    stl_filename: str | None
    remarks: str | None


class LinkedFolderExport(BaseModel):
    """Schema for exporting a linked library folder."""

    name: str


class ProjectExport(BaseModel):
    """Schema for exporting a project."""

    name: str
    description: str | None
    color: str | None
    status: str
    target_count: int | None
    target_parts_count: int | None
    notes: str | None
    tags: str | None
    due_date: datetime | None
    priority: str
    budget: float | None
    bom_items: list[BOMItemExport] = []
    linked_folders: list[LinkedFolderExport] = []


class PrintPlanItemResponse(BaseModel):
    """One file in a project's print plan, with computed per-row totals."""

    id: int
    library_file_id: int
    copies: int
    order_index: int
    # 0 = the whole file (single-plate files, raw gcode); 1..N = that plate
    # of a multi-plate 3MF. Mirrors ProjectPrintPlanItem.plate_index.
    plate_index: int = 0

    # Joined library file fields for display (read-only)
    filename: str
    print_name: str | None = None
    file_type: str
    thumbnail_path: str | None = None
    swap_compatible: bool = False

    # Per-unit metadata (nullable — unsliced 3MFs have no timings)
    filament_grams: float | None = None
    print_time_seconds: int | None = None
    object_count: int | None = None
    cost_per_copy: float | None = None

    # Computed totals = per-unit × copies (null when per-unit is null)
    total_filament_grams: float | None = None
    total_print_time_seconds: int | None = None
    total_objects: int | None = None
    total_cost: float | None = None

    # Per-project print progress (read-only): count of completed
    # ``print_archives`` rows with this ``(project_id, library_file_id)``
    # pair, plus the derived ``copies - printed_count`` remainder
    # (clamped at 0 so an operator-reduced ``copies`` value doesn't
    # surface as a negative).
    printed_count: int = 0
    remaining_count: int = 0

    class Config:
        from_attributes = True


class PrintPlanResponse(BaseModel):
    """Full plan: ordered items plus grand totals across all rows."""

    items: list[PrintPlanItemResponse]
    totals_filament_grams: float = 0.0
    totals_print_time_seconds: int = 0
    totals_objects: int = 0
    totals_cost: float = 0.0
    # Currency-per-kg used when computing cost, echoed back so the UI can
    # label the total without a second round-trip to /settings.
    default_filament_cost_per_kg: float = 0.0


class PrintPlanItemUpdate(BaseModel):
    """Patch a single plan row — only ``copies`` is user-editable here."""

    copies: int


class PrintPlanReorderRequest(BaseModel):
    """Bulk-reorder: list of library_file_ids in the desired display order."""

    library_file_ids: list[int]


class ProjectImport(BaseModel):
    """Schema for importing a project."""

    name: str
    description: str | None = None
    color: str | None = None
    status: str = "active"
    target_count: int | None = None
    target_parts_count: int | None = None
    notes: str | None = None
    tags: str | None = None
    due_date: datetime | None = None
    priority: str = "normal"
    budget: float | None = None
    bom_items: list[BOMItemExport] = []
    linked_folders: list[LinkedFolderExport] = []


class ProjectPartRow(BaseModel):
    """One canonical part in the project ledger, targets merged with history."""

    name: str
    name_key: str
    target_qty: int | None = None  # None = seen in archives but no target set
    printed: int = 0
    in_progress: int = 0
    defective: int = 0
    usable: int = 0
    remaining: int | None = None  # None when there is no target


class ProjectPartsResponse(BaseModel):
    parts: list[ProjectPartRow]


class ProjectPartTargetUpdate(BaseModel):
    name_key: str = Field(min_length=1, max_length=512)
    name: str | None = Field(default=None, max_length=512)  # display name for a row created by hand
    target_qty: int = Field(ge=0)


class ProjectPartsUpdate(BaseModel):
    parts: list[ProjectPartTargetUpdate]
