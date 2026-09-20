"""Read-side helpers shared by every route that shows the stock ledger.

Two routes render a ledger row — the product page's shelf and the farm-wide
Stock tab — and they must render it the SAME way: the order a reservation
belongs to is resolved here, once per page, and the note stays the token the
writer wrote (the frontend translates it). Nothing here writes: the ledger's
single writer is ``services/part_stock.py``.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.schemas.product import StockMovementOut


async def orders_of_lines(db: AsyncSession, line_ids: set[int]) -> dict[int, tuple[int, str]]:
    """``line_id → (order id, order name)`` for a whole page of movements, in ONE join.

    A reservation names an order LINE and nothing else — the ledger has no
    order column, because a line already has one and two would be able to
    disagree. The page still has to show the order the operator recognises, so
    the hop is made here, once for every line on the page rather than once per
    movement.

    A line id that resolves to nothing is simply absent: the caller reads
    ``.get(...)`` and sends ``None``. That is a deleted line whose rows
    ``part_stock.detach_line`` has not reached (PostgreSQL's ``SET NULL`` and
    SQLite's silence differ here), and a movement with no order left is still a
    movement that happened.
    """
    if not line_ids:
        return {}
    rows = await db.execute(
        select(ProjectLine.id, Project.id, Project.name)
        .join(Project, Project.id == ProjectLine.project_id)
        .where(ProjectLine.id.in_(line_ids))
    )
    return {line_id: (order_id, name) for line_id, order_id, name in rows.all()}


def movement_out(
    row: ProductPartStockMovement, names: dict[int, str], orders: dict[int, tuple[int, str]]
) -> StockMovementOut:
    """One ledger row on the wire, with its part named and its order resolved.

    Both maps are the caller's — built once for a whole page — so this stays a
    pure formatter with no query hidden in it.
    """
    order = orders.get(row.project_line_id) if row.project_line_id is not None else None
    return StockMovementOut(
        id=row.id,
        part_id=row.product_part_id,
        part_name=names[row.product_part_id],
        delta=row.delta,
        reason=row.reason,
        project_line_id=row.project_line_id,
        order_id=order[0] if order else None,
        order_name=order[1] if order else None,
        archive_id=row.archive_id,
        note=row.note,
        created_by=row.created_by,
        created_at=row.created_at,
    )
