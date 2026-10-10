"""Finished goods on the wire (spec workshop-finished-goods, rules 16–22).

Every figure is the server's; the frontend draws it. A position's
configuration travels in the same shape as an order line's, so one caption
helper serves both.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.app.models.stock_issue import WAYBILL_MAX
from backend.app.schemas.archive import PaginationMeta
from backend.app.schemas.part_image import PartImageRef
from backend.app.schemas.project import MAX_QTY, LineConfigurationOut, RecipientIn


class StockProductRef(BaseModel):
    id: int
    name: str
    sku: str | None = None
    has_cover: bool = False


class StockItemOut(BaseModel):
    id: int
    code: str
    product: StockProductRef
    configuration: LineConfigurationOut
    location: str | None = None
    on_hand: int
    reserved: int
    available: int
    min_qty: int
    below_min: bool
    #: How many the free quantity is short of the minimum — 0 when it is not below it.
    short_by: int = 0
    #: Whole units of this configuration the free-parts shelf can make.
    can_assemble: int = 0


class StockMoveOut(StockItemOut):
    """The position after a movement. ``moved`` is False when nothing moved — a
    count that matched the shelf (spec rule 10: «без змін»)."""

    moved: bool = True
    #: An issue's dispatch note (spec workshop-dispatch-notes, rule 17); None for every other kind.
    issue_id: int | None = None
    issue_code: str | None = None


class StockSuggestLineIn(BaseModel):
    """One line the dialog asks about (spec workshop-add-to-order, rule 10). ``line_id``
    names an existing line whose own reservation counts as free for it."""

    product_id: int
    options: list[int] = Field(default_factory=list)
    part_counts: dict[int, int] = Field(default_factory=dict)
    quantity: int = Field(ge=1, le=MAX_QTY)
    line_id: int | None = None


class StockSuggestIn(BaseModel):
    items: list[StockSuggestLineIn] = Field(min_length=1, max_length=100)


class StockSuggestLineOut(BaseModel):
    product_id: int
    finished_free: int
    kits_free: int
    from_finished: int
    from_kits: int
    to_print: int
    position_id: int | None = None
    position_code: str | None = None


class StockSuggestOut(BaseModel):
    items: list[StockSuggestLineOut]


class StockItemsPage(BaseModel):
    items: list[StockItemOut]
    meta: PaginationMeta


class StockItemsSummary(BaseModel):
    """The tiles — the whole farm, never the list's filters (WS-01)."""

    on_hand: int = 0
    reserved: int = 0
    available: int = 0
    tracked: int = 0
    below_min: int = 0


class StockReservationOut(BaseModel):
    """A reservation group; ``project_line_id`` None — held without an order."""

    project_line_id: int | None = None
    project_id: int | None = None
    project_code: str | None = None
    # WS-13 E12 F6 (A02): «OR-… · name — N pcs» on the position page.
    project_name: str | None = None
    qty: int


class StockItemSibling(BaseModel):
    id: int
    code: str
    configuration: LineConfigurationOut
    on_hand: int
    available: int


class StockItemPartOut(BaseModel):
    part_id: int
    name: str
    per: int
    on_shelf: int
    #: The part's picture (spec part-thumbnails §11.3) -- filled by ``part_images.fill`` after the
    #: answer is built; ``None`` where the route is not one that shows pictures.
    image: PartImageRef | None = None


class StockItemDetail(StockItemOut):
    reservations: list[StockReservationOut] = []
    siblings: list[StockItemSibling] = []
    parts: list[StockItemPartOut] = []


class StockLookupOut(BaseModel):
    """What a dialog shows for a picked product and options before anything moves."""

    item: StockItemOut | None = None
    configuration: LineConfigurationOut
    can_assemble: int = 0
    #: The configuration's printed parts — per unit and on the free shelf.
    parts: list[StockItemPartOut] = []


StockMoveKind = Literal["receipt", "stocktake", "reserve", "release", "issue"]


class StockMoveIn(BaseModel):
    """``POST /stock/moves`` — a position by id, or a product and its options."""

    kind: StockMoveKind
    item_id: int | None = None
    product_id: int | None = None
    options: list[int] = Field(default_factory=list)
    qty: int | None = Field(default=None, le=MAX_QTY)
    #: Інвентаризація — the counted quantity.
    counted: int | None = Field(default=None, le=MAX_QTY)
    note: str | None = Field(default=None, max_length=500)
    customer_id: int | None = None
    from_reserve: bool = False
    #: An issue (spec workshop-order-issue, rule 16): the waybill, and who takes the goods —
    #: absent, the customer's main contact.
    waybill: str | None = Field(default=None, max_length=WAYBILL_MAX)
    recipient: RecipientIn | None = None


class StockAssembleIn(BaseModel):
    item_id: int | None = None
    product_id: int | None = None
    options: list[int] = Field(default_factory=list)
    qty: int = Field(le=MAX_QTY)
    note: str | None = Field(default=None, max_length=500)


class StockItemParamsIn(BaseModel):
    location: str | None = Field(default=None, max_length=64)
    min_qty: int | None = Field(default=None, le=MAX_QTY)


class StockJournalItemRef(BaseModel):
    id: int
    code: str
    configuration: LineConfigurationOut


class StockJournalCustomer(BaseModel):
    id: int
    name: str


class StockJournalOrder(BaseModel):
    id: int
    code: str
    name: str | None = None


class StockJournalUser(BaseModel):
    id: int
    username: str


class StockJournalIssue(BaseModel):
    """The dispatch note a movement belongs to (spec workshop-dispatch-notes, rule 16)."""

    id: int
    code: str


class StockJournalRow(BaseModel):
    """One movement of either ledger. ``delta`` is a part row's; the two
    ``delta_*`` columns are a finished row's."""

    book: Literal["finished", "parts"]
    id: int
    created_at: datetime
    product_id: int | None = None
    product_name: str | None = None
    item: StockJournalItemRef | None = None
    part_name: str | None = None
    kind: str
    delta: int = 0
    delta_on_hand: int = 0
    delta_reserved: int = 0
    note: str | None = None
    customer: StockJournalCustomer | None = None
    project: StockJournalOrder | None = None
    issue: StockJournalIssue | None = None
    user: StockJournalUser | None = None
    #: A parts-book row's part (plan E4, D1); ``None`` on a finished-goods row.
    part_id: int | None = None
    #: The part's picture (spec part-thumbnails §11.3) -- filled by ``part_images.fill`` after the
    #: answer is built; ``None`` where the route is not one that shows pictures.
    image: PartImageRef | None = None


class StockJournalPage(BaseModel):
    items: list[StockJournalRow]
    #: Set only when the page came back full — a short page is the end.
    next_cursor: str | None = None
    #: WS-13 E1 ST1 — the numbered-page mode's meta; ``None`` in the cursor mode.
    meta: PaginationMeta | None = None


class StockJournalProduct(BaseModel):
    """``GET /stock/journal/products`` — a product the chosen books moved (WS-13 E1 ST2)."""

    id: int
    code: str
    name: str
