from datetime import datetime

from pydantic import BaseModel

from backend.app.schemas.archive import PaginationMeta


class SpoolUsageHistoryResponse(BaseModel):
    id: int
    spool_id: int
    printer_id: int | None = None
    print_name: str | None = None
    weight_used: float
    percent_used: int
    status: str
    cost: float | None = None
    created_at: datetime

    class Config:
        from_attributes = True


# ── Farm-wide History view (2026-09-01) ──────────────────────────────────────
# The paged shape of ``GET /inventory/usage``. Deliberately NOT ``from_attributes``
# off the ORM row: every item carries the spool's identity and the printer's name
# alongside it, resolved in the SAME query (see ``spool_usage_service.list_usage``
# for why they travel with the row instead of being looked up client-side).


class SpoolUsageSpoolRef(BaseModel):
    """The spool a usage row charged, as it is NOW.

    Not as it was when the filament burned: there is no snapshot to show
    instead, and a row whose reel was later renamed should still point at the
    reel you can go and look at.

    ⚠️ **Every field a display-name template can read is here**, not just the
    three the default template happens to use. The name is composed in the
    BROWSER (``utils/spoolName.ts``) against a template the server has no
    business knowing, and a row must read exactly like the same spool does in
    the table and on the cards — a shorter payload would silently drop
    whichever placeholder the operator actually configured.
    """

    id: int
    material: str | None = None
    subtype: str | None = None
    brand: str | None = None
    color_name: str | None = None
    rgba: str | None = None
    slicer_filament_name: str | None = None
    note: str | None = None
    label_weight: float | None = None
    weight_used: float | None = None
    cost_per_kg: float | None = None
    purchase_date: datetime | None = None
    filament_diameter: str | None = None
    lot: int | None = None
    # True when the spool has since been retired — the row stays (see
    # ``build_usage_filters``: a history is never filtered by that), and the
    # client marks it so nobody hunts for the reel on a shelf.
    archived: bool = False


class SpoolUsageListItem(BaseModel):
    id: int
    spool_id: int
    created_at: datetime
    weight_used: float
    percent_used: int
    status: str
    cost: float | None = None
    print_name: str | None = None
    archive_id: int | None = None
    printer_id: int | None = None
    printer_name: str | None = None
    # The client labels a retired printer generically ("Printer 5 (Archived)")
    # rather than by a name that may since have been reused — see
    # ``utils/printerLabel.ts``. That rule needs the flag beside the name.
    printer_archived: bool = False
    # NULL only when the row outlived its spool — ``spool_id`` above still names
    # what it charged. See ``spool_usage_service``: the joins are OUTER because
    # SQLite here never enforces the cascade.
    spool: SpoolUsageSpoolRef | None = None


class SpoolUsageTotals(BaseModel):
    """Across the whole filter, not the page on screen."""

    weight_used: float
    cost: float | None = None


class SpoolUsagePage(BaseModel):
    items: list[SpoolUsageListItem]
    meta: PaginationMeta
    totals: SpoolUsageTotals


class SpoolUsageFacetPrinter(BaseModel):
    id: int
    name: str | None = None
    archived: bool = False


class SpoolUsageFacets(BaseModel):
    statuses: list[str]
    printers: list[SpoolUsageFacetPrinter]
    materials: list[str]
    brands: list[str]
