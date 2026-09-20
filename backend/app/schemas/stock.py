"""Wire shapes of the Stock tab — the farm-wide shelf and its journal.

Read-only: the ledger's single writer is ``services/part_stock.py`` and the
only write the tab offers is the product route's own ``stock/adjust``.
"""

from enum import Enum

from pydantic import BaseModel

from backend.app.schemas.product import StockBalanceOut, StockMovementOut
from backend.app.services.part_stock import REASONS

#: The five reasons as a query-parameter enum, built from the ledger's own
#: tuple so the two cannot drift: an unknown reason is a 422 from validation,
#: with no sentence to translate.
StockReason = Enum("StockReason", {r: r for r in REASONS}, type=str)


class StockReservationOut(BaseModel):
    """An ACTIVE order's line holding kits of this product off the shelf.

    Only active orders are listed: a completed order's kits went out inside the
    units the customer received, and a cancelled order has already released.
    ``kits`` is always > 0 — a line holding nothing is not a reservation.
    """

    line_id: int
    order_id: int
    order_name: str
    kits: int


class StockProductOut(BaseModel):
    id: int
    name: str
    #: The catalog flag rides along so the page can MARK a hidden product; it
    #: never filters — a hidden product's parts are on the shelf all the same.
    is_active: bool
    #: ``catalog`` | ``adhoc_job`` | ``adhoc_plate`` — informational.
    origin: str
    kits_available: int
    #: Counted parts only, in the product's own part order — the same rows the
    #: product page's shelf shows.
    parts: list[StockBalanceOut] = []
    reservations: list[StockReservationOut] = []


class StockSummaryOut(BaseModel):
    """``GET /stock`` — every product with a shelf, kits descending, then name."""

    products: list[StockProductOut] = []


class StockMovementRowOut(StockMovementOut):
    """A ledger row as the farm journal shows it: the product page's row plus
    the product it belongs to, because the journal spans every product."""

    product_id: int
    product_name: str


class StockMovementsPageOut(BaseModel):
    """``GET /stock/movements`` — one keyset page, newest first.

    ``next_before_id`` is the last row's id when the page was full, and
    ``None`` when the ledger is exhausted; the client passes it back as
    ``before_id`` to load the older page.
    """

    items: list[StockMovementRowOut] = []
    next_before_id: int | None = None
