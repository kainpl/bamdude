"""The Stock tab: the farm-wide shelf and its journal, read-only.

Everything here reads the ledger through ``services/part_stock.py``'s batch
readers — one grouped query per question for the whole page, the pass-6
discipline — and nothing here writes (``inv-stock-ledger-single-writer``).
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.product import Product, ProductPart
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.user import User
from backend.app.schemas.product import StockBalanceOut
from backend.app.schemas.stock import (
    StockMovementRowOut,
    StockMovementsPageOut,
    StockProductOut,
    StockReason,
    StockReservationOut,
    StockSummaryOut,
)
from backend.app.services import part_stock
from backend.app.services.stock_views import movement_out, orders_of_lines

router = APIRouter(prefix="/stock", tags=["stock"])


@router.get("", response_model=StockSummaryOut)
async def stock_summary(
    q: str | None = Query(None, max_length=200),
    with_stock: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """Every product with a shelf: its kits, its counted parts' balances, and
    the ACTIVE orders' lines holding its kits in reserve.

    ``with_stock`` (default) keeps a product only while its shelf holds
    anything — a counted part above zero, or a live reservation: kits out on
    loan are still the shelf's business, and a product whose whole stock is
    reserved reads as zero balances with a reservation, never as absent.

    The product select is pre-filtered in SQL to those with at least one
    counted printed part — the EXISTS keeps part-less one-off products (adhoc
    plate products are created with no parts) out of the load.
    """
    stmt = (
        select(Product)
        .options(selectinload(Product.parts))
        .where(Product.parts.any(and_(ProductPart.kind == "printed", ProductPart.qty_per_unit > 0)))
    )
    if q and q.strip():
        stmt = stmt.where(Product.name.ilike(f"%{q.strip()}%"))
    products = [p for p in (await db.execute(stmt)).scalars().all() if any(part_stock.is_counted(pt) for pt in p.parts)]
    if not products:
        return StockSummaryOut(products=[])

    ids = [p.id for p in products]
    balances = await part_stock.balances_for_products(db, ids)

    # Reservations: the lines of ACTIVE orders on these products, then ONE
    # ledger read for all of them — the same read the order pages use.
    line_rows = (
        await db.execute(
            select(ProjectLine.id, ProjectLine.product_id, Project.id, Project.name)
            .join(Project, Project.id == ProjectLine.project_id)
            .where(ProjectLine.product_id.in_(ids), Project.status == "active")
        )
    ).all()
    qty_per_unit = {pt.id: pt.qty_per_unit for p in products for pt in p.parts if part_stock.is_counted(pt)}
    reads = await part_stock.line_ledger_reads(db, [row[0] for row in line_rows], qty_per_unit)
    reservations: dict[int, list[StockReservationOut]] = {}
    for line_id, product_id, order_id, order_name in line_rows:
        kits = reads.reserved_units.get(line_id, 0)
        if kits > 0:
            reservations.setdefault(product_id, []).append(
                StockReservationOut(line_id=line_id, order_id=order_id, order_name=order_name, kits=kits)
            )

    out: list[StockProductOut] = []
    for p in products:
        part_balances = balances.get(p.id, {})
        held = sorted(reservations.get(p.id, []), key=lambda r: (-r.kits, r.order_name.lower()))
        if with_stock and not any(v > 0 for v in part_balances.values()) and not held:
            continue
        out.append(
            StockProductOut(
                id=p.id,
                name=p.name,
                is_active=p.is_active,
                origin=p.origin,
                kits_available=part_stock.kits_available(part_balances, list(p.parts)),
                parts=[
                    StockBalanceOut(
                        part_id=pt.id, name=pt.name, qty_per_unit=pt.qty_per_unit, balance=part_balances[pt.id]
                    )
                    for pt in sorted(p.parts, key=lambda pt: (pt.sort_order, pt.id))
                    if pt.id in part_balances
                ],
                reservations=held,
            )
        )
    out.sort(key=lambda row: (-row.kits_available, row.name.lower()))
    return StockSummaryOut(products=out)


@router.get("/movements", response_model=StockMovementsPageOut)
async def stock_movements(
    product_id: int | None = Query(None),
    part_id: int | None = Query(None),
    reason: StockReason | None = Query(None),
    before_id: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PROJECTS_READ),
):
    """The farm's ledger, newest first, one keyset page at a time.

    ``next_before_id`` is set only when the page came back full — a short page
    IS the end, and the client stops asking. A filter naming nothing yields an
    empty page, not a 404: a journal filter is not a lookup.
    """
    rows = await part_stock.movements_across(
        db,
        product_id=product_id,
        part_id=part_id,
        reason=reason.value if reason is not None else None,
        before_id=before_id,
        limit=limit,
    )
    names = {movement.product_part_id: part_name for movement, part_name, _pid, _pname in rows}
    orders = await orders_of_lines(db, {m.project_line_id for m, *_ in rows if m.project_line_id is not None})
    items = [
        StockMovementRowOut(**movement_out(movement, names, orders).model_dump(), product_id=pid, product_name=pname)
        for movement, _part_name, pid, pname in rows
    ]
    return StockMovementsPageOut(items=items, next_before_id=items[-1].id if len(items) == limit else None)
