"""Order (project) schemas — spec §Data model / §API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

PROJECT_STATUSES = ("active", "completed", "cancelled")
PROJECT_PRIORITIES = ("low", "normal", "high", "urgent")


def validate_http_url(value: str | None) -> str | None:
    """Reject anything that isn't an http(s) URL — it is rendered as ``<a href>``,
    so ``javascript:`` / ``data:`` / ``file:`` would be XSS even through React's
    escaping (#1155).

    Public because it guards every operator-supplied link in the domain, not
    just an order's: ``schemas/product.py`` validates ``source_url`` with it too.
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if not trimmed.lower().startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    return trimmed


def _reject_null(value, info: ValidationInfo):
    """These columns are NOT NULL, so an explicit ``null`` must be a 422.

    A PATCH clears a field by sending ``null``; on a NOT NULL column that
    clearing surfaces as an IntegrityError from the flush — a 500 on malformed
    input. This answers 422 instead, and does NOT fire when the field is absent:
    pydantic does not validate defaults, so an omitted field is still left
    alone. Same shape as ``schemas/customer.py::CustomerUpdate``.
    """
    if value is None:
        raise ValueError(f"{info.field_name} cannot be null")
    return value


def _normalize_material(value: str | None) -> str | None:
    """A line's material is a filament-type TOKEN and is matched against the
    archive's ``filament_type`` case-insensitively — normalising on the way in
    means the comparison never has to care (``order_metrics`` upper-cases the
    other side)."""
    if value is None:
        return None
    token = value.strip().upper()
    return token or None


class ProjectLineCreate(BaseModel):
    product_id: int
    quantity: int = Field(default=1, ge=1)
    material: str | None = Field(default=None, max_length=50)
    color: str | None = Field(default=None, max_length=64)
    note: str | None = None

    @field_validator("material")
    @classmethod
    def _mat(cls, v: str | None) -> str | None:
        return _normalize_material(v)


class ProjectLineUpdate(BaseModel):
    quantity: int | None = Field(default=None, ge=1)
    material: str | None = Field(default=None, max_length=50)
    color: str | None = Field(default=None, max_length=64)
    note: str | None = None
    sort_order: int | None = None

    @field_validator("quantity", "sort_order")
    @classmethod
    def _not_null(cls, v: int | None, info: ValidationInfo) -> int:
        return _reject_null(v, info)

    @field_validator("material")
    @classmethod
    def _mat(cls, v: str | None) -> str | None:
        return _normalize_material(v)


class ProcurementUpdate(BaseModel):
    quantity_acquired: int = Field(ge=0)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    customer_id: int | None = None
    description: str | None = None
    color: str | None = None
    notes: str | None = None
    tags: str | None = None
    due_date: datetime | None = None
    priority: str = "normal"
    price: float | None = Field(default=None, ge=0)
    url: str | None = None
    lines: list[ProjectLineCreate] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return validate_http_url(v)

    @field_validator("priority")
    @classmethod
    def _prio(cls, v: str) -> str:
        if v not in PROJECT_PRIORITIES:
            raise ValueError("invalid priority")
        return v


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    customer_id: int | None = None
    description: str | None = None
    color: str | None = None
    status: str | None = None
    notes: str | None = None
    tags: str | None = None
    due_date: datetime | None = None
    priority: str | None = None
    price: float | None = Field(default=None, ge=0)
    url: str | None = None

    @field_validator("name", "status", "priority")
    @classmethod
    def _not_null(cls, v: str | None, info: ValidationInfo) -> str:
        return _reject_null(v, info)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return validate_http_url(v)


class ProjectDuplicate(BaseModel):
    name: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        """Whitespace-only falls back to the generated name rather than 422ing.

        An empty box in the duplicate dialog means "you pick", which is what the
        old handler said with ``(data.name or "").strip() or _duplicate_name(...)``.
        Normalising here keeps the route's ``data.name or _duplicate_name(...)``
        honest — without it, ``"   "`` is truthy and becomes the copy's name.
        """
        return (v or "").strip() or None


class BatchAddArchives(BaseModel):
    archive_ids: list[int]
    project_line_id: int | None = None


class BatchAddQueueItems(BaseModel):
    queue_item_ids: list[int]


class PartFiguresOut(BaseModel):
    part_id: int
    name: str
    qty_per_unit: int
    need: int
    usable: int
    in_progress: int
    remaining: int
    surplus: int


class ProjectLineResponse(BaseModel):
    id: int
    product_id: int
    product_name: str
    quantity: int
    material: str | None
    color: str | None
    note: str | None
    sort_order: int
    units_printed: int
    # 0.0–1.0, capped server-side (``order_metrics._finish`` /
    # ``project_figures``). An overprinted line reports its excess through
    # ``units_printed`` and each part's ``surplus``, never through this.
    progress: float
    parts: list[PartFiguresOut] = []
    # Every archive attributed to this line, in processing order. One archive
    # may appear under two lines — a plate carrying parts of both products, or a
    # file both hold — so these lists are not a partition of the order's prints.
    archive_ids: list[int] = []


class ProcurementOut(BaseModel):
    part_id: int
    name: str
    need: int
    acquired: int
    remaining: int


class ProjectFiguresOut(BaseModel):
    ordered: int
    printed: int
    complete: int
    remaining: int
    total_time_seconds: int
    total_filament_grams: float
    total_cost: float
    defective: int
    margin: float | None
    # 0.0–1.0, capped server-side (see ``ProjectLineResponse.progress``).
    # An overprinted order reports its excess through ``printed`` against
    # ``ordered``, which stay uncapped.
    progress: float
    other_prints_count: int
    all_printed: bool


class ProjectResponse(BaseModel):
    id: int
    name: str
    customer_id: int | None
    customer_name: str | None
    description: str | None
    color: str | None
    status: str
    notes: str | None
    attachments: list | None
    tags: str | None
    due_date: datetime | None
    priority: str
    price: float | None
    url: str | None
    cover_image_filename: str | None
    created_at: datetime
    updated_at: datetime
    lines: list[ProjectLineResponse]
    procurement: list[ProcurementOut]
    figures: ProjectFiguresOut
    # Prints filed under this order that no line could take (spec §Line
    # resolution step 3), oldest first — the ids behind ``other_prints_count``.
    other_archive_ids: list[int] = []


class ProjectListResponse(BaseModel):
    id: int
    name: str
    customer_id: int | None
    customer_name: str | None
    color: str | None
    status: str
    due_date: datetime | None
    priority: str
    price: float | None
    tags: str | None
    cover_image_filename: str | None
    created_at: datetime
    lines_count: int
    ordered: int
    printed: int
    # 0.0–1.0, capped server-side (see ``ProjectLineResponse.progress``).
    # An overprinted order reports its excess through ``printed`` against
    # ``ordered``, which stay uncapped.
    progress: float
    line_products: list["LineProductOut"] = []


class TimelineEvent(BaseModel):
    event_type: str
    timestamp: datetime
    title: str
    description: str | None = None
    metadata: dict | None = None


# ---------- the print plan (spec pass 3) ----------
#
# One contiguous block: everything the plan endpoints put on the wire. The
# engine's dataclasses (``services/plan_engine.py``) speak in bare part ids
# because they are pure; the wire needs names, so every ``part_id → count`` map
# becomes a list of ``PlanPartCount`` sorted by part id and the route resolves
# the names.


class PlanPartCount(BaseModel):
    part_id: int
    name: str
    count: int


class PlanAlternativeOut(BaseModel):
    """Another plate of the row's line that makes exactly the same counted parts.

    The same part is routinely sliced once per printer model — two files, one
    yield — and the engine's greedy picks one of them, which made the other
    invisible in the plan block. This is that other file: the block offers it as
    a file switch on the row, preselects it when the operator sends the row to a
    printer of its model, and can split the row's count across it, because the
    auto-queue routes an item by ``target_model`` and a file only ever reaches
    the printers it was sliced for.

    The figures are PER PRINT, like the row's. The COUNT is not repeated here on
    purpose: the counted yield is identical by construction, so the row's count
    is the count whichever file is chosen.
    """

    plate_id: int  # ProductPlate.id
    library_file_id: int
    plate_index: int  # 0 = the whole file
    filename: str
    # The short model name the auto-queue routes on, or null when the file names
    # none — which is "we do not know", never "any printer".
    printer_model: str | None = None
    print_time_seconds: int | None = None
    filament_used_grams: float | None = None
    cost: float | None = None
    time_unknown: bool = False


class PlanRowOut(BaseModel):
    """One plate, printed ``count`` times.

    ``print_time_seconds`` / ``filament_used_grams`` / ``cost`` are PER PRINT —
    the count is the multiplier, so the block can re-do its own arithmetic while
    the operator edits the count. ``time_unknown`` says the plate is sliced but
    carries no estimate, i.e. it was ranked on its useful count alone.
    """

    plate_id: int  # ProductPlate.id — NOT the slicer's plate index
    library_file_id: int
    plate_index: int  # 0 = the whole file
    filename: str
    count: int
    useful: list[PlanPartCount]
    print_time_seconds: int | None = None
    filament_used_grams: float | None = None
    cost: float | None = None
    time_unknown: bool = False
    printer_model: str | None = None
    # The line's other candidate plates with the identical counted yield, this
    # one excluded — see ``PlanAlternativeOut``. Empty is the ordinary case.
    alternatives: list[PlanAlternativeOut] = []


class LinePlanOut(BaseModel):
    line_id: int
    product_id: int
    product_name: str
    material: str | None = None
    outstanding_before: list[PlanPartCount] = []
    rows: list[PlanRowOut] = []
    surplus_after: list[PlanPartCount] = []
    # Parts still outstanding that no candidate plate yields at all: the count
    # is what is missing, and there is nothing to print for it yet.
    unsatisfiable: list[PlanPartCount] = []
    candidates: list[int] = []  # ProductPlate ids eligible for this line
    not_sliced: list[int] = []  # ProductPlate ids skipped because not sliced


class PlanTotalsOut(BaseModel):
    prints: int
    print_time_seconds: int | None = None  # null as soon as ONE row has no estimate
    filament_used_grams: float
    # null when the farm has no filament rate OR when no counted row could be
    # costed (a rate exists, but nothing planned carries a weight to price).
    # 0.00 would read as "this plan is free" — see ``plan_engine._totals``.
    cost: float | None = None


class OrderPlanResponse(BaseModel):
    lines: list[LinePlanOut] = []
    totals: PlanTotalsOut
    # The engine's iteration guard stopped the covering of at least one line, so
    # the rows are a PREFIX of the plan: printing all of them still leaves work.
    # It defaults to false because a client that has never heard of the flag
    # must read "not truncated", and because that is what every finished plan
    # says — see ``plan_engine.cover``.
    truncated: bool = False


class PlanEnqueueItem(BaseModel):
    plate_id: int  # ProductPlate.id
    count: int = Field(ge=1, le=999)
    line_id: int


class PlanEnqueueTarget(BaseModel):
    """``auto`` = the auto-queue distributor picks the printer; ``printer`` =
    this printer's own queue. Naming a printer is a ROUTING choice, never a
    dispatch one — nothing here or downstream asks whether it is ready."""

    kind: Literal["auto", "printer"]
    printer_id: int | None = None

    @model_validator(mode="after")
    def _printer_id_belongs_to_the_kind(self) -> "PlanEnqueueTarget":
        """The two kinds are two SHAPES, so the shape refuses a wrong one.

        A hand-written check in the handler answered 400 for the same fact the
        schema already knew, and only for the missing half — an ``auto`` target
        carrying a printer id was accepted and the id silently dropped, which
        reads to the caller as "filed under that printer" and is the opposite of
        what happens. Both halves are a 422 naming ``target``.
        """
        if self.kind == "printer" and self.printer_id is None:
            raise ValueError("A printer target needs printer_id")
        if self.kind == "auto" and self.printer_id is not None:
            raise ValueError("An auto target takes no printer_id — the distributor picks the printer")
        return self


class PlanEnqueueRequest(BaseModel):
    items: list[PlanEnqueueItem] = Field(min_length=1)
    target: PlanEnqueueTarget


class PlanEnqueueCreated(BaseModel):
    line_id: int
    plate_id: int
    queue_item_ids: list[int]


class PlanEnqueueResponse(BaseModel):
    created: list[PlanEnqueueCreated] = []


class LineProductOut(BaseModel):
    """What the order card's cover strip needs about one line's product.

    A filename would be the wrong thing to send: the effective cover may be the
    first picture ATTACHMENT rather than the ``cover_image_filename`` column, and
    the strip fetches ``GET /products/{id}/cover-image`` either way. So the flag,
    not the name — this replaced ``product_cover_filenames`` in pass 4.
    """

    product_id: int
    has_cover: bool


# ``ProjectListResponse`` above annotates ``line_products`` with a forward
# reference so the class can live here, at the end, where the parallel passes'
# edits to this file cannot collide with it.
ProjectListResponse.model_rebuild()
