"""Customers — who an order is for. Lives under the projects permissions:
one domain, no new Permission (spec §API)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.customer import Customer
from backend.app.models.project import Project
from backend.app.models.user import User
from backend.app.schemas.customer import CustomerCreate, CustomerResponse, CustomerUpdate
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
        figures=await customer_figures(db, customer.id),
    )


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
    return [await _response(db, c) for c in rows]


@router.post("", response_model=CustomerResponse)
@router.post("/", response_model=CustomerResponse)
async def create_customer(
    data: CustomerCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_CREATE),
):
    customer = Customer(name=data.name.strip(), contact=data.contact, notes=data.notes)
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
