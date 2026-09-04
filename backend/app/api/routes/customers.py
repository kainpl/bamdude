"""Customers — who an order is for. Lives under the projects permissions:
one domain, no new Permission (spec §API)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
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
        figures = out.setdefault(customer_id, _empty_light_figures().model_dump())
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


@router.get("", response_model=list[CustomerResponse])
@router.get("/", response_model=list[CustomerResponse])
async def list_customers(
    db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.PROJECTS_READ)
):
    rows = (await db.execute(select(Customer).order_by(Customer.name))).scalars().all()
    figures = await _light_figures_by_customer(db)
    return [
        CustomerResponse(
            id=c.id,
            name=c.name,
            contact=c.contact,
            notes=c.notes,
            created_at=c.created_at,
            updated_at=c.updated_at,
            figures=figures.get(c.id) or _empty_light_figures(),
        )
        for c in rows
    ]


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
