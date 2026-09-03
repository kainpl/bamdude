"""Wire shapes for the products API (spec §API, §Data model).

Plate recipes are DERIVED, never stored: :class:`PlateRecipeResponse` is the
serialised form of ``services/product_composition.PlateRecipe``, recomputed on
every read from the linked file's ``file_metadata``.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from backend.app.schemas.project import validate_http_url


def _clean_name(value: str) -> str:
    """Trim, and refuse a name that is nothing but whitespace.

    ``Field(min_length=1)`` already rejects ``""``; it cannot see that ``"   "``
    is the same thing. Create and update both run this, so the two paths cannot
    disagree about what a stored name looks like (same rule as ``customers``).
    """
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("name cannot be blank")
    return trimmed


def _never_null(value, field: str):
    """PATCH clears a field by sending ``null`` — but these columns are NOT NULL,
    so clearing one would surface as an IntegrityError from the flush, i.e. a 500
    on malformed input. A validator answers 422 instead. It does not fire when
    the field is absent: pydantic does not validate defaults."""
    if value is None:
        raise ValueError(f"{field} cannot be null")
    return value


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    notes: str | None = None
    designer: str | None = None
    license: str | None = None
    source_url: str | None = None
    design_id: str | None = None

    @field_validator("name")
    @classmethod
    def _name_is_clean(cls, v: str) -> str:
        return _clean_name(v)

    @field_validator("source_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return validate_http_url(v)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    notes: str | None = None
    designer: str | None = None
    license: str | None = None
    source_url: str | None = None
    design_id: str | None = None
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def _name_is_never_null_and_is_clean(cls, v: str | None) -> str | None:
        return _clean_name(_never_null(v, "name"))

    @field_validator("is_active")
    @classmethod
    def _flag_is_never_null(cls, v: bool | None) -> bool | None:
        return _never_null(v, "is_active")

    @field_validator("source_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return validate_http_url(v)


class ProductDuplicate(BaseModel):
    name: str | None = None


class ProductPartCreate(BaseModel):
    kind: str = Field(pattern="^(printed|purchased)$")
    name: str = Field(min_length=1, max_length=512)
    qty_per_unit: int = Field(default=1, ge=0)
    unit_price: float | None = None
    sourcing_url: str | None = None
    remarks: str | None = None

    @field_validator("name")
    @classmethod
    def _name_is_clean(cls, v: str) -> str:
        return _clean_name(v)

    @field_validator("sourcing_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return validate_http_url(v)


class ProductPartUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=512)
    qty_per_unit: int | None = Field(default=None, ge=0)
    unit_price: float | None = None
    sourcing_url: str | None = None
    remarks: str | None = None
    sort_order: int | None = None

    @field_validator("name")
    @classmethod
    def _name_is_never_null_and_is_clean(cls, v: str | None) -> str | None:
        return _clean_name(_never_null(v, "name"))

    @field_validator("qty_per_unit", "sort_order")
    @classmethod
    def _number_is_never_null(cls, v: int | None) -> int | None:
        return _never_null(v, "value")

    @field_validator("sourcing_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return validate_http_url(v)


class ProductPartMerge(BaseModel):
    source_part_id: int


class ProductPartAlias(BaseModel):
    name_key: str = Field(min_length=1, max_length=512)


class ProductPartResponse(BaseModel):
    id: int
    kind: str
    name: str
    name_key: str
    qty_per_unit: int
    aliases: list[str] = []
    auto: bool = False
    unit_price: float | None = None
    sourcing_url: str | None = None
    remarks: str | None = None
    sort_order: int = 0

    @field_validator("aliases", mode="before")
    @classmethod
    def _aliases(cls, v: list | None) -> list:
        """``ProductPart.aliases`` is NULL for a purchased part — the column is
        printed-only. Reaching the field with ``None`` would fail validation on
        a row that is perfectly valid, so the wire type stays a list either way."""
        return list(v or [])

    class Config:
        from_attributes = True


class PlateYieldEntry(BaseModel):
    part_id: int
    name: str
    count: int


class PlateUnassignedEntry(BaseModel):
    name_key: str
    count: int


class PlateRecipeResponse(BaseModel):
    id: int
    library_file_id: int
    filename: str
    plate_index: int
    sliced: bool
    yield_: list[PlateYieldEntry] = Field(default_factory=list, alias="yield")
    unassigned: list[PlateUnassignedEntry] = []
    materials: list[str] = []
    colors: list[str] = []
    print_time_seconds: int | None = None
    filament_used_grams: float | None = None

    class Config:
        populate_by_name = True


class FileLinkRequest(BaseModel):
    library_file_ids: list[int]


class FolderLinkRequest(BaseModel):
    library_folder_ids: list[int]


class ProductAttachmentOut(BaseModel):
    """One typed attachment (spec §Decisions 3) — the shape m158 already wrote
    for the project templates it converted.

    ``size`` and ``uploaded_at`` are tolerant on purpose: m158 carried over
    legacy project attachments whose entries held neither, so an upgraded farm
    has rows a strict model would 500 the product page over.
    """

    category: str
    filename: str
    original_name: str
    size: int = 0
    sort_order: int = 0
    source: str = "manual"
    source_file_id: int | None = None
    uploaded_at: str | None = None

    @field_validator("size", "sort_order", mode="before")
    @classmethod
    def _missing_number_is_zero(cls, v: int | None) -> int:
        return 0 if v is None else v


class AttachmentOrderRequest(BaseModel):
    category: str
    filenames: list[str]


class CoverPickRequest(BaseModel):
    filename: str = Field(min_length=1)


class ProductListItem(BaseModel):
    id: int
    name: str
    is_active: bool
    cover_image_filename: str | None = None
    # The EFFECTIVE cover — the explicit column or the first picture. The card
    # renders ``GET /products/{id}/cover-image`` on this, never on the column.
    has_cover: bool = False
    parts_count: int = 0
    plates_count: int = 0
    lines_count: int = 0


class ProductResponse(ProductListItem):
    description: str | None = None
    notes: str | None = None
    designer: str | None = None
    license: str | None = None
    source_url: str | None = None
    design_id: str | None = None
    attachments: list[ProductAttachmentOut] = []
    parts: list[ProductPartResponse] = []
    library_file_ids: list[int] = []
    library_folder_ids: list[int] = []
    # All-time units printed across EVERY order of this product (spec §Decisions
    # 7). Computed per request from the order figures, never stored — the number
    # on the product page and the one on the order must not be able to disagree.
    units_printed_total: int = 0
    created_at: datetime
    updated_at: datetime


class CardNote(BaseModel):
    """One thing a card fill did, or refused to do — as a CODE, never prose.

    ⚠️ No English on the wire. The operator reads these in their own language,
    and the frontend owns the translation: it switches on ``code`` and formats
    ``params``. A sentence built here would be untranslatable by the only layer
    that knows the user's locale (i18n rule: en + uk, keys in both).
    """

    code: Literal[
        "file_missing",  # the row outlived its bytes
        "unreadable",  # params: error
        "filled_field",  # params: field
        "replaced_files",  # params: count
        "imported_files",  # params: category, count
        "skipped_extension",  # params: name, ext, category
        "skipped_too_large",  # params: name, size, limit
        "skipped_unreadable",  # params: name
        "skipped_unsaved",  # params: name
        "nothing_to_fill",
    ]
    params: dict[str, str | int] = {}


class RereadResponse(BaseModel):
    """``POST /products/{id}/card/reread``.

    The notes ride beside the product rather than in a header: they are the only
    place the operator learns that a field was left alone because it was theirs,
    or that a file was skipped because its category does not carry that type.
    """

    product: ProductResponse
    notes: list[CardNote] = []
