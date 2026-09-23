"""Customers — who an order is for. Lives under the projects permissions:
one domain, no new Permission (spec §API)."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.customer import Customer
from backend.app.models.project import Project
from backend.app.models.user import User
from backend.app.schemas.customer import (
    CustomerCreate,
    CustomerFigures,
    CustomerListFigures,
    CustomerResponse,
    CustomerUpdate,
)
from backend.app.schemas.listing import CustomerListPage
from backend.app.services.list_paging import (
    SortSpec,
    apply_sql_sort,
    page_meta,
    resolve_sort,
    slice_page,
    sort_computed,
)
from backend.app.services.order_metrics import customer_figures

router = APIRouter(prefix="/customers", tags=["customers"])


async def _response(db: AsyncSession, customer: Customer) -> CustomerResponse:
    return CustomerResponse(
        id=customer.id,
        name=customer.name,
        contact=customer.contact,
        notes=customer.notes,
        created_at=customer.created_at,
        updated_at=customer.updated_at,
        figures=CustomerFigures.model_validate(await customer_figures(db, customer.id)),
    )


def _empty_light_figures() -> CustomerListFigures:
    """A customer with no orders at all: zeros, never a missing key."""
    return CustomerListFigures(projects=0, active=0, completed=0, cancelled=0, total_price=0.0)


async def _light_figures_by_customer(db: AsyncSession) -> dict[int, CustomerListFigures]:
    """Figures for the LIST endpoint, in one grouped query.

    ``customer_figures`` loads a full ``OrderContext`` per PROJECT — several
    queries plus every archive row — which is right for one customer and wrong
    once per row of a list. Everything the list actually shows is counts and a
    price sum, and one GROUP BY answers that for the whole table.

    The archive-derived keys (``ordered`` / ``printed`` / ``total_cost``) are
    deliberately ABSENT rather than zero: an absent key cannot be mistaken for a
    measured zero, and the detail endpoint is where the frontend asks for them.
    Unknown statuses are counted under their own key, as ``customer_figures``
    does, so a status added later shows up instead of vanishing.
    """
    rows = await db.execute(
        select(
            Project.customer_id,
            Project.status,
            func.count(Project.id),
            func.coalesce(func.sum(Project.price), 0.0),
        )
        .where(Project.customer_id.is_not(None))
        .group_by(Project.customer_id, Project.status)
    )
    out: dict[int, dict] = {}
    for customer_id, status, count, price_sum in rows:
        figures = out.get(customer_id)
        if figures is None:
            # Not ``setdefault``: its default is evaluated on EVERY row, so a
            # customer with six statuses built (and threw away) five models.
            figures = out[customer_id] = _empty_light_figures().model_dump()
        figures["projects"] += count
        figures[status] = figures.get(status, 0) + count
        figures["total_price"] += float(price_sum or 0)
    for figures in out.values():
        figures["total_price"] = round(figures["total_price"], 2)
    return {customer_id: CustomerListFigures.model_validate(figures) for customer_id, figures in out.items()}


async def _get_or_404(db: AsyncSession, customer_id: int) -> Customer:
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer


_CUSTOMER_SORT = SortSpec(
    sql={"name": (func.lower(Customer.name), False), "created": (Customer.created_at, False)},
    computed={"orders", "active", "completed", "cancelled", "total_price"},
    default="name-asc",
)
# ``orders`` is the light figures' ``projects`` — every order of the customer.
_CUSTOMER_COMPUTED = {
    "orders": lambda r: r.figures.projects,
    "active": lambda r: r.figures.active,
    "completed": lambda r: r.figures.completed,
    "cancelled": lambda r: r.figures.cancelled,
    "total_price": lambda r: r.figures.total_price,
}


@router.get("", response_model=list[CustomerResponse] | CustomerListPage)
@router.get("/", response_model=list[CustomerResponse] | CustomerListPage)
async def list_customers(
    q: str | None = Query(None, description="With page set: ilike on the name or the contact"),
    sort_by: str | None = Query(None, description="With page set: '<key>-<asc|desc>'; unknown → name-asc"),
    page: int | None = Query(None, ge=1, description="Omit entirely for the legacy flat-array response"),
    per_page: int = Query(24, ge=1, le=200),
    all: bool = Query(False, description="With page set, skip pagination and return every matching row"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """The customers list. ``page`` is the compat switch (the inventory's contract).

    Without it the flat array the customer picker and the orders page's filter
    read — unchanged. With it ``{items, meta}``, ``q`` (name or contact) and
    ``sort_by``. The light figures are one GROUP BY over the whole table either
    way; a computed key (an order count or the price sum) sorts the built rows
    here and slices, a SQL key (``name``, ``created``) pages in the database.
    """
    paged = page is not None
    key, direction, computed = resolve_sort(_CUSTOMER_SORT, sort_by)
    query = select(Customer)
    if not paged:
        query = query.order_by(Customer.name)
    total = 0
    if paged:
        if q:
            needle = f"%{q.strip()}%"
            query = query.where(or_(Customer.name.ilike(needle), Customer.contact.ilike(needle)))
        if not computed:
            total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
            query = apply_sql_sort(query, _CUSTOMER_SORT, key, direction, Customer.id)
            if not all:
                query = query.limit(per_page).offset((page - 1) * per_page)
    rows = (await db.execute(query)).scalars().all()
    figures = await _light_figures_by_customer(db)
    items = [
        CustomerResponse(
            id=c.id,
            name=c.name,
            contact=c.contact,
            notes=c.notes,
            created_at=c.created_at,
            updated_at=c.updated_at,
            # ``.get(default)``, never ``or``: the question is whether the
            # customer HAS a row in the grouped result, not whether the model it
            # holds is truthy — two different questions that happen to agree.
            figures=figures.get(c.id, _empty_light_figures()),
        )
        for c in rows
    ]
    if not paged:
        return items
    if computed:
        items = sort_computed(items, _CUSTOMER_COMPUTED[key], direction, id_fn=lambda r: r.id)
        total = len(items)
        items = slice_page(items, page, per_page, all)
    return CustomerListPage(items=items, meta=page_meta(total, page, per_page, all))


@router.post("", response_model=CustomerResponse)
@router.post("/", response_model=CustomerResponse)
async def create_customer(
    data: CustomerCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_CREATE),
):
    customer = Customer(name=data.name, contact=data.contact, notes=data.notes)
    db.add(customer)
    await db.flush()
    await db.refresh(customer)
    return await _response(db, customer)


@router.get("/{customer_id}", response_model=CustomerResponse)
async def get_customer(
    customer_id: int, db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.PROJECTS_READ)
):
    return await _response(db, await _get_or_404(db, customer_id))


@router.patch("/{customer_id}", response_model=CustomerResponse)
async def update_customer(
    customer_id: int,
    data: CustomerUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_UPDATE),
):
    customer = await _get_or_404(db, customer_id)
    for field_name in ("name", "contact", "notes"):
        if field_name in data.model_fields_set:  # explicit null clears; absent leaves alone
            setattr(customer, field_name, getattr(data, field_name))
    await db.flush()
    await db.refresh(customer)
    return await _response(db, customer)


@router.delete("/{customer_id}")
async def delete_customer(
    customer_id: int, db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.PROJECTS_DELETE)
):
    customer = await _get_or_404(db, customer_id)
    # SQLite does not enforce ON DELETE SET NULL — do it explicitly.
    await db.execute(update(Project).where(Project.customer_id == customer_id).values(customer_id=None))
    await db.delete(customer)
    return {"message": "Customer deleted"}
