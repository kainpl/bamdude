"""Order (project) schemas — spec §Data model / §API."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, ValidationInfo, field_validator, model_validator

from backend.app.models.stock_issue import WAYBILL_MAX
from backend.app.schemas.archive import ArchivePartDefective, ArchivePartRow
from backend.app.schemas.order_auto_eject import AutoEjectSelection, AutoEjectSettings

#: The most one request may put on a line or move on a shelf. Far above any
#: shelf, far below the INTEGER a PostgreSQL column overflows at — a typo is
#: refused, never a 500. ``schemas/finished_stock.py`` and the frontend's
#: ``STOCK_MAX_QTY`` read this one.
MAX_QTY = 1_000_000

PROJECT_STATUSES = ("active", "completed", "cancelled")
PROJECT_PRIORITIES = ("low", "normal", "high", "urgent")
# Set by hand only (spec workshop-order-stage, rule 1); «done» is status=completed.
PROJECT_STAGES = ("prep", "printing", "qc")


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
    #: Whole units to take off the product's free stock instead of printing
    #: (pass 8, Decision 4). NOT a column on ``project_lines`` — the route
    #: turns it into ledger movements, so the handler must exclude it from the
    #: model dump it builds the row from. The number that comes back on
    #: :class:`ProjectLineResponse` is what was actually reserved, which is
    #: less when the shelf emptied between rendering the dialog and pressing OK.
    from_stock_units: int = Field(default=0, ge=0)
    #: Ready units to take off the finished-goods shelf (spec workshop-add-to-order,
    #: rule 12) — clamped like the kits, and like them not a column of the dump.
    from_finished: int = Field(default=0, ge=0)
    #: spec workshop-product-variants, rules 15–17 and 20: set at creation and
    #: never changed. ``choices`` is ``{group_id: option_id}`` (a group left
    #: out takes its standard option); ``part_counts`` is ``{part_id: qty}`` —
    #: the changed counts of a ``product`` line, the wanted counts of a
    #: ``parts`` one. Neither is a column: the route hands both to
    #: ``services/line_config``, and excludes them from the row's dump.
    mode: Literal["product", "parts"] = "product"
    choices: dict[int, int] = Field(default_factory=dict)
    part_counts: dict[int, int] = Field(default_factory=dict)

    @field_validator("material")
    @classmethod
    def _mat(cls, v: str | None) -> str | None:
        return _normalize_material(v)


class LineConfigurationIn(BaseModel):
    """``PUT /projects/{id}/lines/{line_id}/configuration`` (rule 20).

    ``choices`` names only the groups to change; ``part_counts`` is the whole
    set of changed (or, for a parts line, wanted) counts. ``dry_run`` answers
    :class:`LineConfigurationImpact` and writes nothing (rule 14).
    """

    choices: dict[int, int] = Field(default_factory=dict)
    part_counts: dict[int, int] = Field(default_factory=dict)
    dry_run: bool = False


class DroppedPartOut(BaseModel):
    part_id: int
    name: str
    per_before: int
    per_after: int
    printed: int
    queued: int


class LineConfigurationImpact(BaseModel):
    reserved_before: int = 0
    #: Null for a caller without stock:read — what the new kit would get is the shelf's (WS-13 E13 O12).
    reserved_after: int | None = 0
    #: The ready units the line holds and would hold in the new configuration's
    #: position (spec workshop-add-to-order, rule 8).
    finished_before: int = 0
    finished_after: int | None = 0
    dropping: list[DroppedPartOut] = []


class LineChoiceOut(BaseModel):
    group_id: int
    group_name: str
    option_id: int
    option_name: str
    is_default: bool


class LineChangedPartOut(BaseModel):
    part_id: int
    name: str
    qty: int
    #: What the line's chosen configuration gives without the change — for a
    #: parts line, the product's own count per unit.
    standard_qty: int


class LineConfigurationOut(BaseModel):
    """Codes and names only; the frontend composes the caption in the reader's language."""

    choices: list[LineChoiceOut] = []
    changed_parts: list[LineChangedPartOut] = []


class ProjectLineUpdate(BaseModel):
    quantity: int | None = Field(default=None, ge=1)
    material: str | None = Field(default=None, max_length=50)
    color: str | None = Field(default=None, max_length=64)
    note: str | None = None
    sort_order: int | None = None
    #: ``None`` — absent or explicitly null — leaves the reservation alone; a
    #: number REWRITES it (release + reserve in the one transaction). Not in
    #: ``_not_null`` for exactly that reason: unlike ``quantity``, this field
    #: has a meaningful "don't touch it", and the dialog sends the box only
    #: when the operator has a shelf to take from.
    from_stock_units: int | None = Field(default=None, ge=0)
    #: Ready units off the finished-goods shelf — absent leaves them alone, a
    #: number rewrites them (only on an active order; spec workshop-add-to-order, rule 13).
    from_finished: int | None = Field(default=None, ge=0, le=MAX_QTY)

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


#: WS-13 E6 G01: a name is trimmed BEFORE its length is checked — blank is refused,
#: and spaces around 255 characters are not the name. The column is ``String(255)``.
OrderName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
#: G03: the columns' own lengths (``projects.color`` 20, ``projects.url`` 2048) — an
#: overflow is a 422 here rather than a database error on PostgreSQL.
OrderColor = Annotated[str, StringConstraints(max_length=20)]
OrderUrl = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2048)]


class ProjectCreate(AutoEjectSelection):
    auto_eject_enabled: bool = False
    name: OrderName
    customer_id: int | None = None
    # A contact of ``customer_id`` (checked in the route), who receives the order.
    contact_id: int | None = None
    description: str | None = None
    color: OrderColor | None = None
    notes: str | None = None
    tags: str | None = None
    due_date: datetime | None = None
    priority: str = "normal"
    price: float | None = Field(default=None, ge=0)
    url: OrderUrl | None = None
    # Absent → the author of the request (spec workshop-order-stage, rule 10); null → nobody.
    responsible_id: int | None = None
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


class ProjectUpdate(AutoEjectSelection):
    auto_eject_enabled: bool | None = None
    name: OrderName | None = None
    customer_id: int | None = None
    contact_id: int | None = None
    description: str | None = None
    color: OrderColor | None = None
    status: str | None = None
    notes: str | None = None
    tags: str | None = None
    due_date: datetime | None = None
    priority: str | None = None
    price: float | None = Field(default=None, ge=0)
    url: OrderUrl | None = None
    responsible_id: int | None = None

    @field_validator("name", "status", "priority", "auto_eject_enabled", "auto_eject_settings")
    @classmethod
    def _not_null(cls, v: str | None, info: ValidationInfo) -> str:
        return _reject_null(v, info)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return validate_http_url(v)


class ProjectStageUpdate(BaseModel):
    """``PUT /projects/{id}/stage`` — the only way a stage changes (spec workshop-order-stage, rule 4)."""

    stage: Literal["prep", "printing", "qc"]


class OrderAssigneeOut(BaseModel):
    """Someone who may be made responsible for an order — an active user (rule 26)."""

    id: int
    username: str


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

        WS-13 E6 G02: the trimmed name is held to the column's 255 — refused, never
        silently cut (an operator's own name is theirs to shorten).
        """
        name = (v or "").strip() or None
        if name is not None and len(name) > 255:
            raise ValueError("name is at most 255 characters")
        return name


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
    #: WS-13 E4 H02: the part is bound to a variant option (whichever the line chose).
    variant: bool = False
    #: WS-13 E4 H04: this line's parts already waiting in a queue — the map the plan
    #: subtracts (``plan_engine.queued_yield_by_line``), never a second copy of its rule.
    queued: int = 0
    #: WS-13 E6 H01: the part of ``surplus`` still to move — ``PartFigures.bankable``,
    #: the very number ``bank-surplus`` moves (0 for a part without a shelf).
    bankable: int = 0


class LinePurchasedPartOut(BaseModel):
    """WS-13 E4 H03: one purchased part of a line — ``need`` = per × the line's
    STORED quantity, the expression ``procurement_figures`` sums per order."""

    part_id: int
    name: str
    per: int
    need: int
    variant: bool = False


class ProjectLineResponse(BaseModel):
    id: int
    product_id: int
    product_name: str
    #: WS-13 E4 H01 — off the loaded product; a line whose product is gone reads
    #: as none of them: no SKU, ``catalog``, no cover.
    product_sku: str | None = None
    product_origin: Literal["catalog", "adhoc_job", "adhoc_plate"] = "catalog"
    #: The EFFECTIVE cover (``product_files.effective_cover``: the column, else the
    #: first picture), as the order lists answer it.
    product_has_cover: bool = False
    quantity: int
    material: str | None
    color: str | None
    note: str | None
    sort_order: int
    units_printed: int
    # Kits taken off the product's free stock (pass 8, Decision 4), read back
    # from the ledger — so this is what was ACTUALLY reserved, not what the
    # dialog asked for. ``units_printed`` stays prints only; "done" is the two
    # added, which is what ``progress`` already is.
    from_stock_units: int = 0
    #: The split of ``from_stock_units`` (spec workshop-add-to-order, rule 14):
    #: ready units off the finished-goods shelf and kits off the free-parts one.
    from_finished: int = 0
    from_kit_units: int = 0
    #: The line's stock counters (spec workshop-order-issue, rule 23): assembled from its
    #: kits, received from its prints, issued to the customer, and on the shelf under the
    #: order now. A parts line sums its parts.
    assembled: int = 0
    received: int = 0
    issued: int = 0
    held: int = 0
    #: Written off under the order (spec workshop-order-issue-followups, rule 44).
    written_off: int = 0
    #: Capped printed-plus-stock coverage for this line.  A production surplus
    #: stays visible in ``units_printed`` but cannot overfill this number.
    covered_units: int
    # 0.0–1.0, capped server-side (``order_metrics._finish`` /
    # ``project_figures``). An overprinted line reports its excess through
    # ``units_printed`` and each part's ``surplus``, never through this.
    progress: float
    parts: list[PartFiguresOut] = []
    #: WS-13 E4 H03 — the line's purchased parts; how much was bought is the order's
    #: procurement, not the line's.
    purchased: list[LinePurchasedPartOut] = []
    # Every archive attributed to this line, in processing order. One archive
    # may appear under two lines — a plate carrying parts of both products, or a
    # file both hold — so these lists are not a partition of the order's prints.
    archive_ids: list[int] = []
    #: Archives in ``printing`` attributed to this line, and pending queue rows
    #: (both tiers) stamped with this line's id.
    prints_in_progress: int = 0
    prints_queued: int = 0
    # spec workshop-product-variants, rule 20.
    mode: Literal["product", "parts"] = "product"
    config_key: str = ""
    configuration: LineConfigurationOut = Field(default_factory=LineConfigurationOut)


class ProcurementOut(BaseModel):
    part_id: int
    name: str
    need: int
    acquired: int
    remaining: int
    #: WS-13 E1 PR1–PR3. ``planned_cost`` = need × price (``None`` without a price);
    #: ``acquired_cost`` = min(acquired, need) × price — 0.0 when nothing was bought,
    #: ``None`` when something was bought at an unknown price.
    unit_price: float | None = None
    sourcing_url: str | None = None
    planned_cost: float | None = None
    acquired_cost: float | None = None


class ProjectFiguresOut(BaseModel):
    ordered: int
    printed: int
    covered_units: int
    complete: int
    remaining: int
    total_time_seconds: int
    total_filament_grams: float
    total_filament_cost: float
    total_energy_cost: float
    total_cost: float
    defective: int
    margin: float | None
    # 0.0–1.0, capped server-side (see ``ProjectLineResponse.progress``).
    # An overprinted order reports its excess through ``printed`` against
    # ``ordered``, which stay uncapped.
    progress: float
    other_prints_count: int
    all_printed: bool
    # Σ over the lines. ``printed`` and ``ordered`` stay literal — the farm
    # printed this many, the customer ordered that many — and this is the third
    # number the order card shows beside them when it is not zero.
    from_stock_units: int = 0
    # What «Списати надлишок» would still move, in parts (Ruling 30). The button
    # is enabled on exactly this: it used to gate on the surplus, which banking
    # never lowers, so it stayed lit for ever and answered "nothing to bank".
    bankable_surplus: int = 0
    # Archives in ``printing`` under this order, and pending rows of both queue
    # tiers under it — rows on a line AND rows filed under the order alone.
    prints_in_progress: int = 0
    prints_queued: int = 0
    #: WS-13 E1 OR8 — the lines' «issued» / «held» counters summed (the list row's own).
    issued_units: int = 0
    held_units: int = 0
    #: WS-13 E1 PR4–PR7 — purchases beside the prints' ``total_cost`` / ``margin``,
    #: which do not change. ``procurement_cost`` is ``None`` when one bought part has
    #: no price; ``procurement_known_cost`` + ``procurement_partial`` say what is known.
    procurement_cost: float | None = 0.0
    procurement_known_cost: float = 0.0
    procurement_partial: bool = False
    cost_with_procurement: float | None = None
    margin_with_procurement: float | None = None


class ProjectCountsOut(BaseModel):
    """WS-13 E1 CN1 — the order page's tab badges: its «Prints» tab's rows and its
    dispatch notes."""

    prints: int = 0
    issues: int = 0


class OrderContactOut(BaseModel):
    """Who receives the order — a contact of its customer (spec workshop-customers, rule 17)."""

    id: int
    code: str
    name: str | None
    role: str | None
    phone: str | None
    email: str | None


class ProjectResponse(BaseModel):
    auto_eject_settings: AutoEjectSettings = Field(default_factory=AutoEjectSettings)
    auto_eject_enabled: bool = False
    id: int
    code: str
    name: str
    customer_id: int | None
    customer_name: str | None
    contact_id: int | None = None
    contact: OrderContactOut | None = None
    description: str | None
    color: str | None
    status: str
    # The column while active, «done» once completed, none once cancelled (spec workshop-order-stage, rule 2).
    stage: str | None = None
    responsible_id: int | None = None
    responsible_name: str | None = None
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
    counts: ProjectCountsOut = ProjectCountsOut()
    # Prints filed under this order that no line could take (spec §Line
    # resolution step 3), oldest first — the ids behind ``other_prints_count``.
    other_archive_ids: list[int] = []


class ProjectListResponse(BaseModel):
    id: int
    code: str
    name: str
    customer_id: int | None
    customer_name: str | None
    color: str | None
    status: str
    # As on ``ProjectResponse`` (spec workshop-order-stage, rules 2 and 8).
    stage: str | None = None
    responsible_id: int | None = None
    responsible_name: str | None = None
    due_date: datetime | None
    priority: str
    price: float | None
    tags: str | None
    cover_image_filename: str | None
    created_at: datetime
    lines_count: int
    ordered: int
    printed: int
    covered_units: int
    remaining: int
    # Kits this order took off its products' free stock, capped per line and
    # summed — the same number the order page's figures carry, so a card and
    # the page it opens cannot disagree about what is already done. Beside
    # ``printed``, never inside it: one is prints, the other is the shelf.
    from_stock_units: int = 0
    #: Units (a parts line: parts) issued to the customer — «issued X of Y» beside
    #: ``ordered`` (spec workshop-order-issue, rule 23), a sum of the counters.
    issued_units: int = 0
    # Off the same batch as ``ordered``/``printed`` — archives in ``printing``
    # under this order, and pending queue rows of both tiers under it.
    prints_in_progress: int = 0
    prints_queued: int = 0
    #: WS-13 E6 H02: the order's bankable surplus off the same batch — the list's
    #: action menu offers «surplus to stock» only when there is some.
    bankable_surplus: int = 0
    # 0.0–1.0, capped server-side (see ``ProjectLineResponse.progress``).
    # An overprinted order reports its excess through ``printed`` against
    # ``ordered``, which stay uncapped.
    progress: float
    line_products: list["LineProductOut"] = []
    #: WS-13 E1 OR2 — the lines' distinct materials, in line order; a line with no
    #: material («any») adds nothing.
    materials: list[str] = []
    #: WS-13 E1 OR3 — the distinct products, in line order, the whole list (the card
    #: draws the first three and «+N» off its length). ``line_products`` stays per line.
    products: list["LineProductOut"] = []
    #: WS-13 E9 A02 — the order's lines of the product the list is filtered by
    #: (``product_id``), in line order; ``None`` when the list is not filtered by one.
    product_lines: list["ProductLineRef"] | None = None


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
    # null with ``hidden`` for a file the library would not show this caller
    # (WS-13 E1 LV5) — the plate's ids and figures are the plan's all the same.
    filename: str | None = None
    hidden: bool = False
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
    # null with ``hidden`` for a file the library would not show this caller
    # (WS-13 E1 LV5); nothing else on the row depends on who is reading.
    filename: str | None = None
    hidden: bool = False
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
    # This line's pending, unassigned auto-queue rows — the same "still waiting"
    # rule ``plan_engine.queued_yield_by_line`` applies to that table. The order
    # page shows its Rebalance button off it (spec 2026-09-10).
    pending_auto_prints: int = 0


class PlanTotalsOut(BaseModel):
    prints: int
    print_time_seconds: int | None = None  # null as soon as ONE row has no estimate
    filament_used_grams: float
    # null when the farm has no filament rate OR when no counted row could be
    # costed (a rate exists, but nothing planned carries a weight to price).
    # 0.00 would read as "this plan is free" — see ``plan_engine._totals``.
    cost: float | None = None
    #: WS-13 E1 OR9 — Σ the lines' rows: the «Print plan» tab's badge.
    rows: int = 0


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


class RebalanceSkipped(BaseModel):
    item_id: int
    reason: str  # one of services.queue_rebalance.SKIP_REASONS


class RebalanceOut(BaseModel):
    """What a rebalance run did (spec 2026-09-10 §3.3).

    ``cancelled`` is always 0 today — a conversion replaces a row, it never
    deletes one — and exists so a later variant that consolidates prints has a
    place to report. ``skipped`` names every item the run looked at and left,
    with a code the frontend translates.
    """

    converted: int = 0
    created: int = 0
    cancelled: int = 0
    moved_parts: int = 0
    skipped: list[RebalanceSkipped] = []


class StockMovedOut(BaseModel):
    """One part's change of free stock, as the operator is told about it.

    The «5 кришок, 5 колб → у залишок» line of Decision 2, and the same shape
    the archive's "count this print into stock" answers with — one movement is
    one movement whichever button wrote it. ``delta`` is signed for the same
    reason the ledger's is: a reversal is a movement too.
    """

    part_id: int
    name: str
    delta: int


class OrderPrintDefectsIn(BaseModel):
    """What came out bad on one of the order's prints: per part when the print
    has part rows, else one flat count. Absolute values, clamped server-side."""

    parts: list[ArchivePartDefective] | None = None
    defective_count: int | None = Field(default=None, ge=0)


class OrderPrintDefectsOut(BaseModel):
    """⚠️ No ``ledger_refused_parts`` here, deliberately. A print this route can
    reach is FILED under an order, and ``part_stock.adjust_unfiled_print``
    returns an empty result on exactly that condition — so the field was
    structurally always 0 and the toast behind it was dead code. The refusal is
    reported where it can happen: the two plate answers and Telegram's prompt."""

    archive_id: int
    quantity: int
    defective_count: int
    parts: list[ArchivePartRow] = []


class BankSurplusResponse(BaseModel):
    """What «Списати надлишок у залишок» did (Decision 2).

    ``moved`` is aggregated per PART, not per line: two lines of the same
    product bank onto the same shelf, and the operator is told what landed
    there, not the bookkeeping that got it there (the ``project_line_id`` on
    each movement keeps that). ``nothing_to_bank`` is not "``moved`` is empty"
    restated — it is the answer to a second press, which is a success and not
    an error: the surplus was already banked.
    """

    moved: list[StockMovedOut] = []
    nothing_to_bank: bool = False


class ProductLineRef(BaseModel):
    """WS-13 E9 A02 — one line of the filtering product in an order of the list: how
    many, and in which configuration (names, composed by the frontend)."""

    line_id: int
    mode: Literal["product", "parts"]
    quantity: int
    configuration: LineConfigurationOut


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


# ---------- add to order: the batch (spec workshop-add-to-order, rules 11–12) ----------


class BatchStockIn(BaseModel):
    """The operator's own numbers — taken as far as the shelf goes, never refused."""

    from_finished: int = Field(default=0, ge=0, le=MAX_QTY)
    from_kits: int = Field(default=0, ge=0, le=MAX_QTY)


class _BatchLineBase(BaseModel):
    material: str | None = Field(default=None, max_length=50)
    color: str | None = Field(default=None, max_length=64)
    note: str | None = None

    @field_validator("material")
    @classmethod
    def _mat(cls, v: str | None) -> str | None:
        return _normalize_material(v)


class BatchProductLineIn(_BatchLineBase):
    """Kits of a product in a configuration; ``stock`` — the server's proposal or the operator's numbers."""

    kind: Literal["product"]
    product_id: int
    quantity: int = Field(default=1, ge=1, le=MAX_QTY)
    choices: dict[int, int] = Field(default_factory=dict)
    part_counts: dict[int, int] = Field(default_factory=dict)
    stock: Literal["auto"] | BatchStockIn = "auto"


class BatchPartsLineIn(_BatchLineBase):
    """Some parts of a product — one «parts» line (quantity 1, nothing from stock)."""

    kind: Literal["parts"]
    product_id: int
    part_counts: dict[int, int] = Field(min_length=1)


class BatchPlateLineIn(_BatchLineBase):
    """A one-off product from a library file's plate, ``copies`` of it."""

    kind: Literal["plate"]
    library_file_id: int
    plate_index: int = Field(default=0, ge=0)
    copies: int = Field(default=1, ge=1, le=MAX_QTY)
    # A plate's one-off product is reused per file and plate, so its shelf can hold goods: the
    # line asks it like a product line (WS-13 E13 O06) — ``auto`` needs ``stock:move``.
    stock: Literal["auto"] | BatchStockIn = "auto"


BatchLine = Annotated[BatchProductLineIn | BatchPartsLineIn | BatchPlateLineIn, Field(discriminator="kind")]


class BatchLinesIn(BaseModel):
    lines: list[BatchLine] = Field(min_length=1, max_length=100)


class LineIntakeOut(BaseModel):
    """What a line asked of the shelf and what it got — less when the shelf moved."""

    line_id: int
    asked_finished: int = 0
    got_finished: int = 0
    asked_kits: int = 0
    got_kits: int = 0


class BatchLinesOut(BaseModel):
    order: ProjectResponse
    results: list[LineIntakeOut]


# ---------- the issue dialog (spec workshop-order-issue, rules 18–19) ----------


class RecipientIn(BaseModel):
    """Who takes the goods — copied into the issue as text."""

    name: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=255)
    delivery_method: str | None = Field(default=None, max_length=255)
    delivery_details: str | None = Field(default=None, max_length=255)


class RecipientOut(BaseModel):
    name: str | None = None
    phone: str | None = None
    delivery_method: str | None = None
    delivery_details: str | None = None


class FulfilmentPartIn(BaseModel):
    part_id: int
    receive: int = Field(default=0, ge=0, le=MAX_QTY)
    issue: int = Field(default=0, ge=0, le=MAX_QTY)
    #: Held parts written off (spec workshop-order-issue-followups, rule 46).
    write_off: int = Field(default=0, ge=0, le=MAX_QTY)


class FulfilmentLineIn(BaseModel):
    line_id: int
    assemble: int = Field(default=0, ge=0, le=MAX_QTY)
    receive: int = Field(default=0, ge=0, le=MAX_QTY)
    issue: int = Field(default=0, ge=0, le=MAX_QTY)
    #: Held units written off (spec workshop-order-issue-followups, rule 46).
    write_off: int = Field(default=0, ge=0, le=MAX_QTY)
    #: A parts line's numbers, part by part.
    parts: list[FulfilmentPartIn] = Field(default_factory=list, max_length=500)


class FulfilmentIn(BaseModel):
    lines: list[FulfilmentLineIn] = Field(default_factory=list, max_length=500)
    recipient: RecipientIn = Field(default_factory=RecipientIn)
    waybill: str | None = Field(default=None, max_length=WAYBILL_MAX)
    note: str | None = Field(default=None, max_length=2000)
    complete: bool = False
    #: Why the batch writes something off — required when it does (followups, rule 46).
    write_off_note: str | None = Field(default=None, max_length=2000)


class PartStateOut(BaseModel):
    part_id: int
    name: str
    wanted: int
    can_receive: int
    held: int
    issued: int
    written_off: int = 0


class StockPositionRefOut(BaseModel):
    """WS-13 E6 H04: where a line's configuration is kept — «комірка» in the issue dialog."""

    id: int
    code: str
    location: str | None = None


class LineStateOut(BaseModel):
    line_id: int
    product_name: str
    mode: Literal["product", "parts"]
    ordered: int
    from_finished: int
    kits_reserved: int
    can_assemble: int
    can_receive: int
    held: int
    issued: int
    written_off: int = 0
    parts: list[PartStateOut] = []
    #: WS-13 E6 H04: the line's configuration with names — two lines of one product
    #: are told apart by it. Built off the same context ``state`` read.
    configuration: LineConfigurationOut | None = None
    #: The stock position of the line's (product, configuration); None for a parts
    #: line or a configuration nobody has kept yet.
    stock_position: StockPositionRefOut | None = None


class FulfilmentStateOut(BaseModel):
    lines: list[LineStateOut]
    ordered: int
    issued: int
    held: int
    fully_issued: bool
    #: What a batch could assemble, receive and issue now, over the whole order.
    can_assemble: int = 0
    can_receive: int = 0
    can_issue: int = 0
    #: No customer: the order closes into free stock (spec workshop-order-issue-followups, rule 35).
    closes_to_stock: bool = False
    #: Can the order be closed now — fully issued, or fully on the shelf when it closes to stock.
    can_complete: bool = False
    #: The order's contact person, else the customer's main contact (rule 18).
    # Null for a caller who neither keeps the contacts nor ships the goods (WS-13 E13 O25).
    recipient: RecipientOut | None = None


class FulfilmentOut(BaseModel):
    order: ProjectResponse
    #: The issue this batch opened; None when it issued nothing.
    issue_id: int | None = None
    #: Its dispatch note's code, «DN-0042».
    issue_code: str | None = None
    #: WS-13 E6 H03: the units the sealed note carries (``issue.units``); None without an issue.
    issue_units: int | None = None


# ---------- «take from stock» (spec workshop-order-issue, rules 17, 20) ----------


class StockOfferOut(BaseModel):
    line_id: int
    product_name: str
    from_finished: int
    kits: int


class TakeStockLineIn(BaseModel):
    """What the banner SHOWED for a line — the take is clamped to it and to the shelf."""

    line_id: int
    from_finished: int = Field(default=0, ge=0, le=MAX_QTY)
    kits: int = Field(default=0, ge=0, le=MAX_QTY)


class TakeStockIn(BaseModel):
    #: None or absent: take the current offers.
    lines: list[TakeStockLineIn] | None = Field(default=None, max_length=500)


class TakeStockOut(BaseModel):
    order: ProjectResponse
    results: list[LineIntakeOut]
