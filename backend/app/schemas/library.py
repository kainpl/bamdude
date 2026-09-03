"""Pydantic schemas for library (File Manager) functionality."""

from datetime import datetime

from pydantic import BaseModel, Field

from backend.app.schemas.archive import PaginationMeta
from backend.app.schemas.calibration_mode import CalibrationMode
from backend.app.schemas.timelapse import TimelapseStorage


class ProductRef(BaseModel):
    """Tiny product reference embedded in file/folder responses — enough for a
    chip; the full shape lives in ``backend.app.schemas.product``."""

    id: int
    name: str
    is_active: bool = True

    class Config:
        from_attributes = True


# ============ Folder Schemas ============


class FolderCreate(BaseModel):
    """Schema for creating a new folder."""

    name: str = Field(..., min_length=1, max_length=255)
    parent_id: int | None = None
    # Products this folder belongs to. Empty list = no product links.
    product_ids: list[int] = Field(default_factory=list)


class ExternalFolderCreate(BaseModel):
    """Schema for linking an external folder."""

    name: str = Field(..., min_length=1, max_length=255)
    external_path: str = Field(..., min_length=1, max_length=500)
    readonly: bool = True
    show_hidden: bool = False
    parent_id: int | None = None


class FolderUpdate(BaseModel):
    """Schema for updating a folder.

    ``product_ids``: ``None`` = leave links untouched, ``[]`` = unlink
    from every product, otherwise replace the whole list.
    """

    name: str | None = Field(None, min_length=1, max_length=255)
    parent_id: int | None = None
    product_ids: list[int] | None = None


class FolderResponse(BaseModel):
    """Schema for folder response."""

    id: int
    name: str
    parent_id: int | None
    # M2M product links. Empty list = unattached.
    products: list[ProductRef] = Field(default_factory=list)
    is_external: bool = False
    external_path: str | None = None
    external_readonly: bool = False
    external_show_hidden: bool = False
    file_count: int = 0  # Computed field
    # max(folder.updated_at, max(immediate-child file.updated_at)). Used by the
    # File Manager folder tree's "sort by recent activity" mode (#1770) so that
    # adding a file inside a folder bubbles it up — folder.updated_at alone only
    # tracks rename/move events. Recursion across subfolders is intentionally
    # left out to keep the route a single GROUP BY rather than a recursive CTE.
    latest_activity_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class FolderReadmeResponse(BaseModel):
    """Markdown sidebar payload for a folder (#1268).

    ``filename`` is the on-disk name (so the UI can show "README.md") and
    ``content`` is the raw markdown — the FE renders it. ``truncated`` is
    True when the source file was clipped at the size cap.
    """

    filename: str
    content: str
    truncated: bool


class FolderTreeItem(BaseModel):
    """Schema for folder tree item (includes children)."""

    id: int
    name: str
    parent_id: int | None
    products: list[ProductRef] = Field(default_factory=list)
    is_external: bool = False
    external_path: str | None = None
    external_readonly: bool = False
    file_count: int = 0
    # See FolderResponse.latest_activity_at — #1770 folder sort source.
    latest_activity_at: datetime | None = None
    children: list["FolderTreeItem"] = []

    class Config:
        from_attributes = True


# ============ File Schemas ============


class FileUpdate(BaseModel):
    """Schema for updating a file.

    ``product_ids``: ``None`` = leave links untouched, ``[]`` = unlink
    from every product, otherwise replace the whole list.
    """

    filename: str | None = Field(None, min_length=1, max_length=255)
    folder_id: int | None = None
    product_ids: list[int] | None = None
    notes: str | None = None


class FileDuplicate(BaseModel):
    """Reference to a duplicate file."""

    id: int
    filename: str
    folder_id: int | None
    folder_name: str | None
    created_at: datetime


class FileResponse(BaseModel):
    """Schema for file response."""

    id: int
    folder_id: int | None
    folder_name: str | None = None
    # M2M product links — empty list = unattached.
    products: list[ProductRef] = Field(default_factory=list)
    is_external: bool = False

    filename: str
    file_path: str
    file_type: str
    # Composite tag array driving frontend badges + chip-row filter
    # (m036). Computed by ``services.library_helpers.compute_file_tags``;
    # stays consistent with ``file_type`` / ``file_metadata`` /
    # ``source_type`` / ``swap_compatible``. See ``LibraryFile.file_tags``
    # for the value vocabulary.
    file_tags: list[str] = []
    file_size: int
    file_hash: str | None
    thumbnail_path: str | None

    metadata: dict | None

    last_printed_at: datetime | None
    print_count: int = 0

    notes: str | None

    # Provenance (m033) — populated for MakerWorld imports + slicer outputs.
    # ``source_type`` ∈ {"makerworld", "sliced", ...}; ``source_url`` is the
    # canonical link (e.g. MakerWorld profile URL). NULL for plain uploads.
    source_type: str | None = None
    source_url: str | None = None

    # Duplicate detection
    duplicates: list[FileDuplicate] | None = None
    duplicate_count: int = 0

    # User tracking (Issue #206)
    created_by_id: int | None = None
    created_by_username: str | None = None

    created_at: datetime
    updated_at: datetime
    # See FileListResponse.fs_modified_at — m129 / #2680.
    fs_modified_at: datetime | None = None

    # Metadata fields
    print_name: str | None = None
    print_time_seconds: int | None = None
    filament_used_grams: float | None = None
    object_count: int | None = None
    # gcode_label_objects AND exclude_object — badge in the file list, and the
    # preview banner explains what it means. Denormalised column (m114), not a
    # JSON dig, so the list stays filterable server-side.
    skip_objects_supported: bool = False
    sliced_for_model: str | None = None
    swap_compatible: bool = False
    # True iff the file has 2+ plates (extracted at upload / m023). Frontend
    # uses this to gate gallery rendering — saves an N-card-page worth of
    # /plates fetches when most files are single-plate.
    is_multi_plate: bool = False

    class Config:
        from_attributes = True


class TagSummary(BaseModel):
    """Minimal tag reference embedded in file list responses (#1268).

    DISTINCT from ``FileListResponse.file_tags`` (list[str] of computed
    system badges, m036) — this carries user-authored tag rows.
    """

    id: int
    name: str

    class Config:
        from_attributes = True


class TagResponse(BaseModel):
    """A library tag catalog row with its file usage count (#1268)."""

    id: int
    name: str
    file_count: int
    # Handed out by the system, not editable. ``code`` is the stable identifier
    # the frontend styles and translates by; NULL on user tags.
    is_system: bool = False
    code: str | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TagCreate(BaseModel):
    """Create a new library tag."""

    name: str = Field(..., min_length=1, max_length=64)


class TagUpdate(BaseModel):
    """Rename an existing library tag."""

    name: str = Field(..., min_length=1, max_length=64)


class TagBulkAssignRequest(BaseModel):
    """Add / remove / replace tag assignments across multiple files (#1268)."""

    file_ids: list[int] = Field(..., min_length=1)
    tag_ids: list[int] = Field(default_factory=list)
    action: str = Field("add", pattern="^(add|remove|replace)$")


class TagBulkAssignResponse(BaseModel):
    """Result of a bulk tag-assignment operation (#1268)."""

    files_updated: int
    associations_added: int
    associations_removed: int


class FileListResponse(BaseModel):
    """Schema for file list item (lighter than full response)."""

    id: int
    folder_id: int | None
    # M2M product IDs only (names omitted to keep list payload small —
    # frontend resolves names from a global ``products`` query).
    product_ids: list[int] = Field(default_factory=list)
    is_external: bool = False
    filename: str
    file_type: str
    # Composite tag array — see ``FileResponse.file_tags``.
    file_tags: list[str] = []
    file_size: int
    thumbnail_path: str | None
    duplicate_count: int = 0
    # User tracking (Issue #206)
    created_by_id: int | None = None
    created_by_username: str | None = None
    created_at: datetime
    # Real on-disk mtime, external files only (m129 / #2680). NULL for uploads,
    # where ``created_at`` already IS the moment the file arrived. The list
    # renders and sorts on ``fs_modified_at ?? created_at``: a bulk external
    # scan stamps one identical ``created_at`` across the whole batch, so
    # sorting an external folder by date without this is sorting on a tie.
    fs_modified_at: datetime | None = None

    # Key metadata fields for display
    print_name: str | None = None
    print_time_seconds: int | None = None
    filament_used_grams: float | None = None
    object_count: int | None = None
    # gcode_label_objects AND exclude_object — badge in the file list, and the
    # preview banner explains what it means. Denormalised column (m114), not a
    # JSON dig, so the list stays filterable server-side.
    skip_objects_supported: bool = False
    sliced_for_model: str | None = None
    swap_compatible: bool = False
    is_multi_plate: bool = False
    # Provenance (m033) — same semantics as ``FileResponse``. List endpoint
    # surfaces them so the file card can show the "MakerWorld" / "Sliced"
    # badge without a follow-up detail fetch.
    source_type: str | None = None
    source_url: str | None = None
    # Number of notes attached (gh#3) - drives the card icon variant
    # (MessageSquarePlus when 0, MessageSquare when >0).
    notes_count: int = 0
    # Successful completions only — the increment in ``_bump_library_file_usage``
    # is gated on status, so a file attempted and failed reads as 0.
    print_count: int = 0
    # #1268 — user-authored tags (M2M). DISTINCT from ``file_tags`` above,
    # which is the computed system-badge array (m036).
    tags: list["TagSummary"] = []

    class Config:
        from_attributes = True


class LibraryFileListPage(BaseModel):
    """Paginated envelope for ``GET /library/files`` (task 1, 2026-08-29
    server-driven lists) — returned only when the request carries ``page``.

    Mirrors ``PaginatedArchiveResponse``'s ``meta`` (same ``PaginationMeta``
    field names: total / current_page / per_page / last_page) so both list
    endpoints read the same way on the frontend; the item container is named
    ``items`` here rather than archives' ``data`` per this endpoint's own
    contract. Omitting ``page`` entirely still returns the legacy flat
    ``list[FileListResponse]`` — this model never appears in that path.
    """

    items: list[FileListResponse]
    meta: PaginationMeta


class FileMoveRequest(BaseModel):
    """Schema for moving files to a folder."""

    file_ids: list[int]
    folder_id: int | None = None  # None = move to root


class FilePrintRequest(BaseModel):
    """Schema for printing a file from the library.

    Note: printer_id is passed as a query parameter, not in the body.
    """

    # Print options (same as archive reprint)
    plate_id: int | None = None
    plate_name: str | None = None
    ams_mapping: list[int] | None = None
    # Tri-state calibration (off/auto/on) or legacy bool.
    bed_levelling: CalibrationMode = "on"
    flow_cali: CalibrationMode = "off"
    layer_inspect: bool = False
    timelapse: bool = False
    # Which medium records it — only offered when the machine has both.
    timelapse_storage: TimelapseStorage | None = None
    use_ams: bool = True
    nozzle_offset_cali: CalibrationMode = "on"  # Dual-nozzle printers only — MQTT-gated (#1682)
    mesh_mode_fast_check: bool = True
    # Opt this print into per-model auto-print G-code injection (#1516). When on
    # with quantity > 1, ALL copies queue so every one is injected by the
    # scheduler. No-op unless snippets exist for the target model.
    gcode_injection: bool = False
    execute_swap_macros: bool = True
    swap_macro_events: list[str] | None = None
    selected_macro_ids: list[int] | None = None
    # Batch: first copy dispatches now, remaining (quantity-1) queue up
    quantity: int = 1
    # Project to associate the resulting archive with (when triggered from project view)
    project_id: int | None = None
    # The order line this print is for; travels queue → dispatcher → archive.
    project_line_id: int | None = None
    # When true, delete the LibraryFile row + disk file after the archive has
    # been created and the print has been dispatched. Used by the Printers-page
    # Direct-Print flow (click / drag-drop a file onto a printer card) so the
    # transient upload doesn't linger in File Manager. Cleanup is skipped on
    # external library files. Upstream #730 / #1682b695.
    cleanup_library_after_dispatch: bool = False


class FileUploadResponse(BaseModel):
    """Schema for file upload response."""

    id: int
    filename: str
    file_type: str
    # Composite tag array — see ``FileResponse.file_tags``. Surfaced on
    # the upload response so the frontend can update its file list with
    # the right badge composition without a follow-up GET.
    file_tags: list[str] = []
    file_size: int
    thumbnail_path: str | None
    duplicate_of: int | None = None  # the row that was used instead, when one was
    # What actually happened to the bytes that were sent. ``created`` is a new
    # row; ``deduped`` means the content was already here and ``id`` is that
    # existing row; ``restored`` means the row existed but had lost its file and
    # these bytes put it back. A substitution the user cannot see reads as an
    # upload that did nothing, so every surface says which of the three it was.
    outcome: str = "created"
    # The name the upload was going to carry, kept only when it differs from the
    # row that was used — "you sent X, we used Y" needs both halves.
    superseded_name: str | None = None
    metadata: dict | None = None


# ============ Bulk Operations ============


class BulkDeleteRequest(BaseModel):
    """Schema for bulk delete operations."""

    file_ids: list[int] = []
    folder_ids: list[int] = []


class BulkDeleteResponse(BaseModel):
    """Schema for bulk delete response."""

    deleted_files: int
    deleted_folders: int
    # What the caller asked for and did not get. Files skipped for ownership or
    # because a queue item is mid-print were previously counted nowhere, so the
    # interface reported them as deleted.
    skipped_files: int = 0


# ============ Queue Operations ============


class ZipExtractResult(BaseModel):
    """Result for a single file extracted from ZIP."""

    filename: str
    file_id: int
    folder_id: int | None = None


class ZipExtractError(BaseModel):
    """Error for a file that couldn't be extracted."""

    filename: str
    error: str


class ZipExtractResponse(BaseModel):
    """Schema for ZIP extraction response."""

    extracted: int
    # Entries whose content the library already held, so no row was created and
    # the extracted bytes were removed again. Reported so a ZIP that adds
    # nothing does not read as an extraction that silently failed.
    skipped_duplicates: int = 0
    folders_created: int
    files: list[ZipExtractResult]
    errors: list[ZipExtractError]


# ============ STL Thumbnail Generation ============


class BatchThumbnailRequest(BaseModel):
    """Schema for batch STL thumbnail generation request."""

    file_ids: list[int] | None = None
    folder_id: int | None = None
    all_missing: bool = False


class BatchThumbnailResult(BaseModel):
    """Result for a single file thumbnail generation."""

    file_id: int
    filename: str
    success: bool
    error: str | None = None


class BatchThumbnailResponse(BaseModel):
    """Schema for batch thumbnail generation response."""

    processed: int
    succeeded: int
    failed: int
    results: list[BatchThumbnailResult]


# ============ Queue Sequencer Grouping ============


class LibraryGroupingPlate(BaseModel):
    """One plate, reduced to what decides which group it belongs to."""

    index: int
    # Sorted so two plates that need the same filaments compare equal without
    # the caller having to normalise. ⚠️ TYPES only — colour is never part of a
    # grouping key, and a colour field here would invite one.
    filament_types: list[str]
    bed_type: str | None = None


class LibraryGroupingMetadata(BaseModel):
    """Everything the queue sequencer needs to group a file's plates.

    Read from ``LibraryFile.file_metadata`` alone — no disk access — which is
    what lets a 60-file selection be grouped in one query.
    """

    file_id: int
    filename: str
    sliced_for_model: str | None = None
    nozzle_diameter: float | None = None
    bed_type: str | None = None
    # Empty for a file that was never parsed (raw STL, unsliced 3MF). The caller
    # must treat that as "cannot be grouped", never as "matches anything".
    plates: list[LibraryGroupingPlate] = []


# ============ Model card of a library file (spec §Decisions 5) ============


class CardAuxOut(BaseModel):
    """One file inside an ``Auxiliaries/`` folder, plus the url that serves it.

    ``url`` names WHICH of the two card routes can serve this member, and the
    server decides: ``card-file`` for a picture the browser will render (append
    a camera stream token — an ``<img src>`` cannot carry an Authorization
    header), ``card-download`` for everything else (an ordinary bearer read, so
    a customer's bill of materials never sits behind a long-lived kiosk token).
    Built server-side because the ZIP path needs percent-encoding and because
    that split is a server rule the frontend should not re-derive.
    """

    name: str
    zip_path: str
    size: int = 0
    url: str


class CardResponse(BaseModel):
    """What a 3MF says about itself — the ``CardData`` dataclass on the wire.

    Read from the file on disk on every request, NOT from ``file_metadata``,
    which carries only ``designer`` and ``print_name``. ``error`` is set when the
    file could not be parsed; the card screen degrades, the request still
    succeeds (``ThreeMFCardParser.parse`` never raises).
    """

    title: str | None = None
    description: str | None = None
    designer: str | None = None
    designer_user_id: str | None = None
    license: str | None = None
    copyright: str | None = None
    creation_date: str | None = None
    modification_date: str | None = None
    origin: str | None = None
    profile_title: str | None = None
    profile_description: str | None = None
    profile_cover: str | None = None
    profile_user_id: str | None = None
    profile_user_name: str | None = None
    design_model_id: str | None = None
    design_profile_id: str | None = None
    design_region: str | None = None
    # Every category the parser knows is always present, empty when the 3MF has
    # no such folder, so the frontend can index without guarding.
    auxiliaries: dict[str, list[CardAuxOut]] = {}
    error: str | None = None
