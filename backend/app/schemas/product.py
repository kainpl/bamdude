"""Wire shapes for the products API (spec §API, §Data model).

Plate recipes are DERIVED, never stored: :class:`PlateRecipeResponse` is the
serialised form of ``services/product_composition.PlateRecipe``, recomputed on
every read from the linked file's ``file_metadata``.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

from backend.app.schemas.farm_forecast import EstimateReasonOut
from backend.app.schemas.part_image import ImageChoice, InstanceImageRef, PartImageRef
from backend.app.schemas.project import validate_http_url


def _clean_name(value: Any) -> Any:
    """Trim, and refuse a name that is nothing but whitespace.

    ``Field(min_length=1)`` already rejects ``""``; it cannot see that ``"   "``
    is the same thing. Create and update both run this, so the two paths cannot
    disagree about what a stored name looks like (same rule as ``customers``).

    ⚠️ Every caller wires it with ``mode="before"``, so the field's own
    ``min_length`` / ``max_length`` measure what will be STORED. Run after the
    constraints — as this module did until the fix ``schemas/customer.py`` got
    was carried across — they were decoration on one side and a lie on the
    other: ``"   "`` satisfied ``min_length=1`` and only this function caught
    it, while a full-length name typed with a trailing space was a 422 for a
    length the very next statement was about to remove.

    A non-string goes straight through: the field's own type check answers
    those, and raising here would be a 500 rather than a 422.
    """
    if not isinstance(value, str):
        return value
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("name cannot be blank")
    return trimmed


def _blank_to_none(value: Any) -> Any:
    """A stripped text field, or None when nothing is left of it."""
    if isinstance(value, str):
        return value.strip() or None
    return value


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
    # Bounded by their columns (WS-13 E10 A03): longer reached PostgreSQL as a 500.
    designer: str | None = Field(default=None, max_length=255)
    license: str | None = Field(default=None, max_length=255)
    source_url: str | None = Field(default=None, max_length=2048)
    design_id: str | None = Field(default=None, max_length=64)
    # spec workshop-product-catalog, rules 1 and 13–15. A blank SKU / version
    # is no value; the SKU's uniqueness and the ready gate are the route's.
    sku: str | None = Field(default=None, max_length=64)
    version: str | None = Field(default=None, max_length=64)
    category_id: int | None = None
    status: Literal["draft", "ready"] = "draft"

    @field_validator("name", mode="before")
    @classmethod
    def _name_is_clean(cls, v: Any) -> Any:
        return _clean_name(v)

    @field_validator("sku", "version", mode="before")
    @classmethod
    def _blank_is_none(cls, v: Any) -> Any:
        return _blank_to_none(v)

    @field_validator("source_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return validate_http_url(v)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    notes: str | None = None
    designer: str | None = Field(default=None, max_length=255)
    license: str | None = Field(default=None, max_length=255)
    source_url: str | None = Field(default=None, max_length=2048)
    design_id: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None
    # One-way promotion (spec Decision 2): the only value accepted is
    # ``catalog``; an adhoc product never becomes adhoc again, and the
    # Literal is what makes 422 the answer to anything else.
    origin: Literal["catalog"] | None = None
    # spec workshop-product-catalog, rules 1 and 13–15. A blank SKU / version
    # is no value; the SKU's uniqueness and the ready gate are the route's.
    sku: str | None = Field(default=None, max_length=64)
    version: str | None = Field(default=None, max_length=64)
    category_id: int | None = None
    status: Literal["draft", "ready"] | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name_is_never_null_and_is_clean(cls, v: Any) -> Any:
        return _clean_name(_never_null(v, "name"))

    @field_validator("is_active")
    @classmethod
    def _flag_is_never_null(cls, v: bool | None) -> bool | None:
        return _never_null(v, "is_active")

    @field_validator("origin")
    @classmethod
    def _origin_is_never_null(cls, v: str | None) -> str | None:
        return _never_null(v, "origin")

    @field_validator("status")
    @classmethod
    def _status_is_never_null(cls, v: str | None) -> str | None:
        return _never_null(v, "status")

    @field_validator("sku", "version", mode="before")
    @classmethod
    def _blank_is_none(cls, v: Any) -> Any:
        return _blank_to_none(v)

    @field_validator("source_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return validate_http_url(v)


class ProductDuplicate(BaseModel):
    name: str | None = Field(default=None, max_length=255)


class ProductPartCreate(BaseModel):
    kind: str = Field(pattern="^(printed|purchased)$")
    name: str = Field(min_length=1, max_length=512)
    qty_per_unit: int = Field(default=1, ge=0)
    #: «Не рахувати» (spec workshop-order-issue-followups, rule 34) — only with a zero.
    ignored: bool = False
    unit_price: float | None = None
    sourcing_url: str | None = Field(default=None, max_length=512)
    remarks: str | None = None
    #: The whole alias list of a printed part, normalised by the route — its own key
    #: always stays (WS-13 E10 A05). A purchased part takes none.
    aliases: list[Annotated[str, Field(max_length=512)]] | None = None
    #: Created already bound to this option: the new part enters the final composition
    #: — the lines and positions that chose it — with nothing frozen (A05, K22).
    variant_option_id: int | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name_is_clean(cls, v: Any) -> Any:
        return _clean_name(v)

    @field_validator("sourcing_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return validate_http_url(v)


class ProductPartUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=512)
    qty_per_unit: int | None = Field(default=None, ge=0)
    unit_price: float | None = None
    sourcing_url: str | None = Field(default=None, max_length=512)
    remarks: str | None = None
    sort_order: int | None = None
    #: Absent or ``null`` — the aliases stay; a list — the whole list, own key kept
    #: (WS-13 E10 A05). Written beside the other fields, in the same transaction.
    aliases: list[Annotated[str, Field(max_length=512)]] | None = None
    #: The option this part belongs to, or ``null`` for a part in every
    #: configuration (spec workshop-product-variants, rule 19). The route
    #: refuses an option of another product.
    variant_option_id: int | None = None
    #: «Не рахувати» (spec workshop-order-issue-followups, rule 34). The route refuses it on
    #: a part in the kit (422) and on one that holds stock or is ordered (409).
    ignored: bool | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name_is_never_null_and_is_clean(cls, v: Any) -> Any:
        return _clean_name(_never_null(v, "name"))

    @field_validator("qty_per_unit", "sort_order", "ignored")
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
    variant_option_id: int | None = None
    ignored: bool = False
    # How many of this part are on the shelf (pass 8, Decision 6). A SUM over
    # the ledger, never a column — ``models/part_stock`` says why — so every
    # route answering with a part reads it, and ``0`` here means "no stock",
    # not "not asked". A purchased part or one marked «не рахувати» has no
    # balance to have and reads 0 for good.
    # Null for a caller without stock:read (WS-13 E13 O12).
    stock_balance: int | None = 0
    #: The part's picture (spec part-thumbnails §11.3) -- filled by ``part_images.fill`` after the
    #: answer is built; ``None`` where the route is not one that shows pictures.
    image: PartImageRef | None = None
    #: The editor's state (spec §10.4) -- only on the editor's routes.
    image_choice: ImageChoice | None = None

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
    #: The part's picture (spec part-thumbnails §11.3) -- filled by ``part_images.fill`` after the
    #: answer is built; ``None`` where the route is not one that shows pictures.
    image: PartImageRef | None = None


class PlateUnassignedEntry(BaseModel):
    name_key: str
    count: int
    #: The smallest id of the name on this plate; ``None`` when the file names its objects without
    #: ids (plan E4, D2). Its picture is that object's.
    identify_id: int | None = None
    image: InstanceImageRef | None = None


class PlateRecipeResponse(BaseModel):
    id: int
    library_file_id: int
    #: WS-13 E1 K5 / LV4 — ``None`` with ``hidden`` for a file the library would not
    #: show this caller; the plate's numbers stay (Z7).
    filename: str | None
    hidden: bool = False
    plate_index: int
    sliced: bool
    yield_: list[PlateYieldEntry] = Field(default_factory=list, alias="yield")
    unassigned: list[PlateUnassignedEntry] = []
    materials: list[str] = []
    colors: list[str] = []
    # The model the plate was sliced for, normalised (``X1C``) — None when the file says nothing.
    printer_model: str | None = None
    print_time_seconds: int | None = None
    filament_used_grams: float | None = None

    class Config:
        populate_by_name = True


class PartSourceOut(BaseModel):
    """One plate a part can be printed from (WS-13 E1 PS1) — ``filename`` / folder
    ``None`` with ``hidden`` for a file the library would not show this caller."""

    plate_id: int
    library_file_id: int
    filename: str | None = None
    folder_id: int | None = None
    folder_name: str | None = None
    hidden: bool = False
    plate_index: int
    printer_model: str | None = None
    sliced: bool
    yield_: int = Field(alias="yield")
    print_time_seconds: int | None = None
    filament_used_grams: float | None = None
    #: The first sliced source, in the plan's order.
    recommended: bool = False

    class Config:
        populate_by_name = True


class PartSourcesOut(BaseModel):
    """PS6 — a part's sources and what they add up to: ``yield_*`` over the sliced
    ones, visible and hidden alike; ``hidden_sources`` counted by the server."""

    part_id: int
    sources: list[PartSourceOut] = []
    has_sliced_source: bool = False
    yield_min: int | None = None
    yield_max: int | None = None
    hidden_sources: int = 0


class ProductSourcesOut(BaseModel):
    parts: list[PartSourcesOut] = []


class ProductFileOut(BaseModel):
    """PS7 — one file linked to the product, with its plates."""

    library_file_id: int
    filename: str | None = None
    hidden: bool = False
    folder_id: int | None = None
    folder_name: str | None = None
    file_type: str | None = None
    #: A container a plan can use (3MF / gcode); STL / STEP must be sliced first.
    plan_eligible: bool = False
    printer_model: str | None = None
    sliced_any: bool = False
    plates: list[PlateRecipeResponse] = []
    #: WS-13 E9 A03 — a 3MF container (``.3mf``, case aside — the library's rule), the
    #: files a card can be re-read from; known for a file without access too.
    is_3mf: bool = False
    #: WS-13 E9 A03 — the file sits in a folder linked to this product NOW. The server keeps
    #: no history of how a file joined; the page unlinks such a file through its folder.
    in_linked_folder: bool = False


class ProductFolderOut(BaseModel):
    """WS-13 E9 A03 — one folder linked to the product. Named for any library reader
    (the library's folder routes' rule); for nobody else."""

    folder_id: int
    name: str | None = None
    hidden: bool = False


class ProductFilesOut(BaseModel):
    files: list[ProductFileOut] = []
    hidden_files: int = 0
    #: WS-13 E9 A03 — the linked folders, named first by name, hidden after by id.
    folders: list[ProductFolderOut] = []


class EstimateSurplusOut(BaseModel):
    part_id: int
    name: str
    count: int


class ProductEstimateOut(BaseModel):
    """WS-13 E1 ES — ONE unit of the standard configuration printed from scratch, in
    whole plates. ``complete`` is exactly «no reasons»; a missing filament rate is the
    farm's setting, said by ``filament_cost = None``, never a reason."""

    prints: int
    print_time_seconds: int | None = None
    #: The known part: a row without grams adds nothing and is a reason.
    filament_grams: float = 0.0
    filament_cost: float | None = None
    surplus: list[EstimateSurplusOut] = []
    purchased_cost: float | None = None
    purchased_known_cost: float = 0.0
    purchased_partial: bool = False
    complete: bool
    reasons: list[EstimateReasonOut] = []


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

    ``source`` is one of the closed set ``product_files.SOURCE_VALUES`` —
    ``manual`` (the upload route), ``3mf`` (``fill_from_file``) or ``import``
    (``import_zip``) — and every writer uses those constants. The WIRE type is
    still a plain ``str`` for the same reason as the two above: a hand-edited
    column or a restored backup carrying a fourth value must render the page,
    not 500 it. The closed set is enforced where it can be enforced — at the
    writers, by ``test_product_files.py`` — not at the reader, where the only
    thing a rejection can do is take the page down.
    """

    # ⚠️ ``category`` and ``original_name`` are DEFAULTED for the same reason
    # ``size`` is: the docstring above promises tolerance of what m158 carried
    # over, and a legacy row missing either would 500 the whole product page
    # rather than render one unlabelled attachment. ``filename`` has no default
    # on purpose — an entry that names no file is not an attachment, and
    # ``_rows`` drops it before this model ever sees it.
    category: str = "other"
    filename: str
    original_name: str = ""
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


class ProductCategoryRef(BaseModel):
    id: int
    name: str


# ---------- variants (spec workshop-product-variants, rules 1–3, 18) ----------


class VariantOptionOut(BaseModel):
    id: int
    name: str
    position: int = 0
    #: Order lines that chose this option, and parts bound to it — what a delete
    #: would be refused over, so the card can grey the button out beforehand.
    lines_count: int | None = 0
    parts_count: int = 0
    #: Finished-goods positions whose configuration chose it (spec workshop-finished-goods, rule 14).
    stock_count: int | None = 0


class VariantGroupOut(BaseModel):
    id: int
    name: str
    position: int = 0
    default_option_id: int | None = None
    options: list[VariantOptionOut] = []
    #: What would refuse the group's delete, counted by the server exactly as the delete
    #: guard counts (WS-13 E1 VR9): lines and positions with a choice in it, parts bound
    #: to one of its options.
    lines_count: int | None = 0
    stock_count: int | None = 0
    parts_count: int = 0


class VariantOptionDraftIn(BaseModel):
    """One option of a variants draft: an existing ``id`` (renamed in place) or a new
    ``temp_id`` — exactly one of them (WS-13 E1 VR5)."""

    id: int | None = None
    temp_id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=128)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: Any) -> Any:
        return _clean_name(v)


class VariantGroupDraftIn(BaseModel):
    id: int | None = None
    temp_id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    options: list[VariantOptionDraftIn] = Field(default_factory=list, max_length=200)
    #: The standard option — an ``id`` or a ``temp_id`` of THIS group's options; ``null``
    #: only for an existing group whose stored standard is already ``null`` (WS-13 E10 A06).
    default: int | str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: Any) -> Any:
        return _clean_name(v)


class VariantsApplyIn(BaseModel):
    """``PUT /products/{id}/variants`` — the desired final state, in order, and the
    revision the client saw (WS-13 E1 VR4–VR5)."""

    revision: str = Field(max_length=128)
    groups: list[VariantGroupDraftIn] = Field(default_factory=list, max_length=50)


def _clean_variant_names(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    return [_clean_name(v) for v in value]


class VariantGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    #: The first one is the standard. At least one — checked by the route, so
    #: the refusal is a sentence rather than a validation list.
    options: list[str] = []

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: Any) -> Any:
        return _clean_name(v)

    @field_validator("options", mode="before")
    @classmethod
    def _options(cls, v: Any) -> Any:
        return _clean_variant_names(v)

    @field_validator("options")
    @classmethod
    def _option_length(cls, v: list[str]) -> list[str]:
        if any(len(name) > 128 for name in v):
            raise ValueError("option name is too long")
        return v


class VariantGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    default_option_id: int | None = None
    position: int | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: Any) -> Any:
        return _clean_name(_never_null(v, "name"))

    @field_validator("default_option_id", "position")
    @classmethod
    def _not_null(cls, v: int | None) -> int | None:
        return _never_null(v, "value")


class VariantOptionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: Any) -> Any:
        return _clean_name(v)


class VariantOptionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    position: int | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: Any) -> Any:
        return _clean_name(_never_null(v, "name"))

    @field_validator("position")
    @classmethod
    def _not_null(cls, v: int | None) -> int | None:
        return _never_null(v, "position")


class ListVariantOption(BaseModel):
    id: int
    name: str


class ListVariantGroup(BaseModel):
    """A catalog row's variant group (WS-13 E5 H01): enough for the add-to-order
    dialog to name the options of an unpicked row and fill a picked row's selects."""

    id: int
    name: str
    #: ``None`` — the group has no standard option, and the server leaves it unchosen.
    default_option_id: int | None = None
    options: list[ListVariantOption] = []


class ProductListItem(BaseModel):
    id: int
    code: str
    name: str
    is_active: bool
    # spec workshop-product-catalog, rule 13.
    sku: str | None = None
    version: str | None = None
    category: ProductCategoryRef | None = None
    status: Literal["draft", "ready"] = "draft"
    cover_image_filename: str | None = None
    # The EFFECTIVE cover — the explicit column or the first picture. The card
    # renders ``GET /products/{id}/cover-image`` on this, never on the column.
    has_cover: bool = False
    parts_count: int = 0
    plates_count: int = 0
    # The order counts and the stock figures below are null for a caller who may not read
    # that domain (WS-13 E13 O12).
    lines_count: int | None = 0
    # Whole units the free stock can already make (pass 8, Decision 6) — the
    # scarcest counted part decides, because a kit is the unit the operator
    # thinks in. On the LIST too, and read there for every product in one
    # aggregated query: a per-product read would be an N+1 behind the catalog.
    kits_available: int | None = 0
    #: Ready units free across the product's finished-goods positions (spec
    #: workshop-add-to-order, rule 15) — one grouped read per page.
    finished_available: int | None = 0
    #: The product's plate materials, colours and printer models (stored facets,
    #: files outside the trash only), sorted — the add-to-order dialog's selects and chips.
    materials: list[str] = []
    colors: list[str] = []
    models: list[str] = []
    #: WS-13 E1 PC2 — a linked file outside the trash can be printed as it is.
    sliced: bool = False
    #: PC3 — printed parts that are parts (a zero in the kit counts, «not counted»
    #: does not), purchased parts, the variant groups' names in their order,
    #: DISTINCT active orders with a line of the product, and its ready-goods
    #: positions: tracked ones and those below their minimum.
    printed_parts_count: int = 0
    purchased_parts_count: int = 0
    variant_group_names: list[str] = []
    #: WS-13 E5 H01 — the groups with their options, in ``position``/``id`` order;
    #: one grouped read per page. The detail overrides it with the full groups.
    variant_groups: list[ListVariantGroup] = []
    active_orders_count: int | None = 0
    finished_positions: int | None = 0
    finished_below_min: int | None = 0
    # ``catalog`` | ``adhoc_job`` | ``adhoc_plate`` (models.product.ProductOrigin).
    origin: str = "catalog"
    origin_file_id: int | None = None
    origin_plate_index: int | None = None


class ProductResponse(ProductListItem):
    #: The variants as the client sees them, hashed (WS-13 E1 VR4) — what
    #: ``PUT …/variants`` checks its draft against.
    variants_revision: str = ""
    description: str | None = None
    notes: str | None = None
    designer: str | None = None
    license: str | None = None
    source_url: str | None = None
    design_id: str | None = None
    attachments: list[ProductAttachmentOut] = []
    parts: list[ProductPartResponse] = []
    variant_groups: list[VariantGroupOut] = []
    library_file_ids: list[int] = []
    library_folder_ids: list[int] = []
    # All-time units printed across EVERY order of this product (spec §Decisions
    # 7). Computed per request from the order figures, never stored — the number
    # on the product page and the one on the order must not be able to disagree.
    units_printed_total: int | None = 0
    #: WS-13 E9 A01 — the tab counts: attachments outside the gallery (not ``pictures``)
    #: and DISTINCT orders with a line of this product, whatever their status.
    documents_count: int = 0
    orders_count: int | None = 0
    #: A copy made without the library's right to link left the source's files and folders
    #: behind (WS-13 E13 CAT-08); only a duplicate's answer sets it.
    links_skipped: bool = False
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
        # Shared by the 3MF fill and the ZIP import — the same three questions
        # ("wrong type", "too big", "could not write it") get the same three
        # answers whichever container the file arrived in. The import's cover
        # variants add ``category = "cover"``, which no fill ever produces.
        "skipped_extension",  # params: name, ext, category
        # params: name, size, limit, category — where category is one of
        # ATTACHMENT_CATEGORIES, "cover" (the dedicated cover), or "files" (a
        # library member over the per-member cap). A card FILL emits it without
        # a category at all; the frontend must not require one.
        "skipped_too_large",
        "skipped_unreadable",  # params: name
        "skipped_unsaved",  # params: name (+ category on the cover)
        "nothing_to_fill",
        # Import-only (spec §Decisions 6).
        "import_file_missing",  # params: name
        "import_file_refused",  # params: name, detail — detail is the LIBRARY's own words
        "import_part_duplicate_key",  # params: name, key
        "import_plate_missing",  # params: filename, plate_index
        "import_bad_category",  # params: name, category
        "import_attachment_missing",  # params: name
        "import_bad_name",  # params: name
        "import_cover_missing",
    ]
    params: dict[str, str | int] = {}


class ProductImportResponse(BaseModel):
    """``POST /products/import``.

    ⚠️ ``warnings`` are :class:`CardNote` codes, exactly like a card fill's —
    no English on the wire. The first version of this shipped prose, on the
    argument that an import warning is mostly untranslatable data (a filename,
    a category BamDude does not have) with only a short fixed half around it.
    That argument is wrong twice: the fixed half is the sentence the operator
    actually reads, and a locale that gets half its product page translated and
    half not is worse than either. The one string that survives as a string is
    ``import_file_refused``'s ``detail`` — the library's own rejection message,
    passed through verbatim rather than re-invented as a second vocabulary for
    the same refusals (``store_library_upload``'s docstring makes the same
    point about the Telegram bot showing ``e.detail``).
    """

    product: ProductResponse
    warnings: list[CardNote] = []


class RereadResponse(BaseModel):
    """``POST /products/{id}/card/reread``.

    The notes ride beside the product rather than in a header: they are the only
    place the operator learns that a field was left alone because it was theirs,
    or that a file was skipped because its category does not carry that type.
    """

    product: ProductResponse
    notes: list[CardNote] = []


class ProductFromFileResponse(BaseModel):
    """``POST /products/from-file/{library_file_id}`` (WS-13 E10 A02) — the new product and
    what its file gave, the way the re-read answers; the notes used to reach only the log."""

    product: ProductResponse
    notes: list[CardNote] = []


# ---------- free stock (pass 8, Decision 6) ----------


class ProductPartVariantOut(BaseModel):
    """The option a part is bound to — «angled tail» belongs to «Tail: angled»."""

    group: str
    option: str


class StockBalanceOut(BaseModel):
    """One counted part and what is on the shelf for it.

    ``qty_per_unit`` rides along because the page's whole question is "how many
    kits", and that is ``balance // qty_per_unit`` per part — sending the
    balance alone would make the frontend ask for the product a second time to
    divide by a number it already had.
    """

    part_id: int
    name: str
    qty_per_unit: int
    balance: int
    #: Held by orders' parts lines — neither free nor reserved (spec
    #: workshop-order-issue-followups, rule 49). Only the product page's stock asks it;
    #: the stock tab's rows leave it null rather than claim a zero nobody counted.
    held_for_orders: int | None = None
    #: WS-13 E1 ST4 — the option the part is bound to, when it is.
    variant: ProductPartVariantOut | None = None
    #: The part's picture (spec part-thumbnails §11.3) -- filled by ``part_images.fill`` after the
    #: answer is built; ``None`` where the route is not one that shows pictures.
    image: PartImageRef | None = None


class KitsByOptionOut(BaseModel):
    """WS-13 E1 ST4 / Q12 — the kits the free shelf makes with ONE option of one group,
    every other group at its standard."""

    group_id: int
    group_name: str
    option_id: int
    option_name: str
    is_default: bool
    kits: int


class ProductKitsOut(BaseModel):
    """``GET /products/{id}/kits`` — whole kits of ONE configuration the free
    stock can make (spec workshop-product-variants): what a line with those
    options and counts could take off the shelf."""

    kits_available: int = 0


class StockMovementOut(BaseModel):
    """One row of the ledger, as the product page reads it.

    ``order_id`` / ``order_name`` are RESOLVED here rather than left to the
    client: a reservation names an order LINE, and the page has no way to turn
    a line id into the order the operator recognises. Both are ``None`` for a
    movement whose line was detached — a deleted line keeps its history but has
    no order any more (``part_stock.detach_line``).

    ``note`` is a TOKEN from ``part_stock.NOTE_TOKENS`` for everything the
    backend writes and the operator's own words for a hand correction; the
    frontend translates the first kind and prints the second (Ruling 17).
    """

    id: int
    part_id: int
    part_name: str
    delta: int
    reason: str
    project_line_id: int | None = None
    order_id: int | None = None
    order_name: str | None = None
    archive_id: int | None = None
    note: str | None = None
    created_by: int | None = None
    created_at: datetime
    #: The part's picture (spec part-thumbnails §11.3) -- filled by ``part_images.fill`` after the
    #: answer is built; ``None`` where the route is not one that shows pictures.
    image: PartImageRef | None = None


class ProductStockOut(BaseModel):
    """``GET /products/{id}/stock`` — the shelf and how it got that way."""

    balances: list[StockBalanceOut] = []
    kits_available: int = 0
    #: WS-13 E1 ST5 — the stock page's own helper, for this product.
    kits_by_option: list[KitsByOptionOut] = []
    #: Newest first, capped by the request's ``limit`` (200 by default, 500 at
    #: most). Deliberately NOT filtered to counted parts: a movement written
    #: before a part was zeroed still happened, and a history that hides it is
    #: not a history.
    movements: list[StockMovementOut] = []


class StockAdjustIn(BaseModel):
    """``POST /products/{id}/stock/adjust`` — the operator's hand correction.

    ``note`` is REQUIRED, unlike every other movement's: the backend's own
    movements say what they were for by their reason, and a hand correction
    says nothing at all unless the person making it does. It is the one note on
    the wire that is prose rather than a token, because only the operator knows
    why the shelf and the ledger disagreed.
    """

    part_id: int
    delta: int
    note: str = Field(min_length=1, max_length=500)

    @field_validator("delta")
    @classmethod
    def _delta_moves_something(cls, v: int) -> int:
        # ``move`` answers a zero delta with ``None`` and writes nothing, so a
        # route that let one through would have to answer 200 with no movement
        # — a shape the response model cannot even express.
        if v == 0:
            raise ValueError("delta cannot be zero")
        return v

    @field_validator("note", mode="before")
    @classmethod
    def _note_is_clean(cls, v: Any) -> Any:
        # Trimmed BEFORE ``min_length`` / ``max_length`` measure it, for the
        # reason ``_clean_name`` gives: otherwise "   " passes a length rule it
        # does not meet and a full-length note is refused over a trailing space.
        if not isinstance(v, str):
            return v
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("note cannot be blank")
        return trimmed
