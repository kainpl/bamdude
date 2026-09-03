"""Order (project) schemas — spec §Data model / §API."""

from datetime import datetime

from pydantic import BaseModel, Field, ValidationInfo, field_validator

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
    progress: float
    line_products: list["LineProductOut"] = []


class TimelineEvent(BaseModel):
    event_type: str
    timestamp: datetime
    title: str
    description: str | None = None
    metadata: dict | None = None


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
