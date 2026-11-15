"""The History view's feed in Spoolman mode — filter / sort / totals / facets over
``spoolman_usage_history`` (m228).

The same contract as ``spool_usage_service`` (the built-in inventory's feed):
one list of conditions drives the page, the count, the totals and the facets,
and nothing is decided in the browser. Two differences, both because the spool
lives in Spoolman and not in our SQL:

* a row names its spool by the SNAPSHOT it carries (material, brand, colour …
  as they were when the charge landed), so the view needs no Spoolman round
  trip and a row reads the same after its spool is deleted there;
* every query is fenced to ONE source token — the current Spoolman's
  (``spoolman_usage_history.source_token``). Rows of an earlier Spoolman
  address are history of another database and are shown nowhere.

There is no "archived spool" filter here: whether a spool is archived is
Spoolman's to say, per request, and a history is not filtered by it anyway.

This module only reads; ``services/spoolman_usage_history`` is the one writer.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.printer import Printer
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.models.spoolman_usage_history import SpoolmanUsageHistory as Row

_SEARCH_COLUMNS = (
    Row.print_name,
    Row.brand,
    Row.material,
    Row.color_name,
    Row.subtype,
    Row.slicer_filament_name,
    Printer.name,
    cast(Row.spoolman_spool_id, String),
)

# Newest first; ``id`` breaks the tie so paging over rows of one second is stable.
_DEFAULT_ORDER = (Row.created_at.desc(), Row.id.desc())

_SPOOL_SORT_COLUMNS = (
    func.coalesce(Row.material, ""),
    func.coalesce(Row.brand, ""),
    func.coalesce(Row.color_name, ""),
)


def _with_joins(query):
    """OUTER: a row outlives its printer (SQLite runs no FK actions)."""
    return query.outerjoin(Printer, Printer.id == Row.printer_id)


def _sort_columns() -> dict[str, Any]:
    return {
        "created_at": Row.created_at,
        "weight_used": Row.weight_used,
        "percent_used": Row.percent_used,
        "cost": func.coalesce(Row.cost, 0),
        "status": Row.status,
        "print_name": func.coalesce(Row.print_name, ""),
        "printer": func.coalesce(Printer.name, ""),
    }


def _order_by(sort_by: str | None) -> list:
    """``<column>_asc`` / ``_desc``; anything else keeps newest-first. Always ends in the id."""
    tiebreak = Row.id.desc()
    if not sort_by:
        return list(_DEFAULT_ORDER)
    sort_key, _, sort_dir = sort_by.rpartition("_")
    if sort_dir not in ("asc", "desc"):
        return list(_DEFAULT_ORDER)
    if sort_key == "spool":
        cols = _SPOOL_SORT_COLUMNS
        return [*([c.asc() for c in cols] if sort_dir == "asc" else [c.desc() for c in cols]), tiebreak]
    column = _sort_columns().get(sort_key)
    if column is None:
        return list(_DEFAULT_ORDER)
    return [column.asc() if sort_dir == "asc" else column.desc(), tiebreak]


def build_filters(
    *,
    source_token: str | None,
    statuses: list[str] | None = None,
    printer_id: str | None = None,
    spool_id: int | None = None,
    material: str | None = None,
    brand: str | None = None,
    assigned: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    q: str | None = None,
) -> list:
    """The shared WHERE list. ``source_token`` is not optional: without a current
    source there is nothing to show, and the fence matches no row."""
    filters: list = [Row.source_token == (source_token or "")]

    if statuses:
        filters.append(Row.status.in_(statuses))

    if printer_id == "__none__":
        filters.append(Row.printer_id.is_(None))
    elif printer_id is not None:
        filters.append(Row.printer_id == int(printer_id))

    if spool_id is not None:
        filters.append(Row.spoolman_spool_id == spool_id)
    if material:
        filters.append(Row.material == material)
    if brand:
        filters.append(Row.brand == brand)

    assignment_exists = (
        select(SpoolmanSlotAssignment.spoolman_spool_id)
        .where(SpoolmanSlotAssignment.spoolman_spool_id == Row.spoolman_spool_id)
        .exists()
    )
    if assigned == "assigned":
        filters.append(assignment_exists)
    elif assigned == "unassigned":
        filters.append(~assignment_exists)

    if date_from is not None:
        filters.append(Row.created_at >= date_from)
    if date_to is not None:
        filters.append(Row.created_at < date_to)

    if q and q.strip():
        # Each token must match at least one column; every token must match something.
        for token in q.strip().split():
            term = f"%{token}%"
            filters.append(or_(*(col.ilike(term) for col in _SEARCH_COLUMNS)))

    return filters


async def list_usage(
    db: AsyncSession, *, filters: list, sort_by: str | None = None, limit: int | None = None, offset: int = 0
) -> list[tuple[Row, Printer | None]]:
    query = _with_joins(select(Row, Printer).select_from(Row)).where(*filters).order_by(*_order_by(sort_by))
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return [(row, printer) for row, printer in (await db.execute(query)).all()]


async def count_usage(db: AsyncSession, *, filters: list) -> int:
    query = _with_joins(select(func.count()).select_from(Row)).where(*filters)
    return int((await db.execute(query)).scalar_one())


async def usage_totals(db: AsyncSession, *, filters: list) -> dict[str, float | None]:
    """Grams and money across the whole filter; ``cost`` None when no row carries one."""
    query = _with_joins(select(func.coalesce(func.sum(Row.weight_used), 0.0), func.sum(Row.cost)).select_from(Row))
    weight, cost = (await db.execute(query.where(*filters))).one()
    return {"weight_used": float(weight or 0.0), "cost": None if cost is None else float(cost)}


async def usage_facets(db: AsyncSession, *, filters: list) -> dict[str, list]:
    """Distinct dropdown values over the source's rows (the route passes only the fence)."""
    statuses = list(
        (await db.execute(select(Row.status).where(*filters).distinct().order_by(Row.status))).scalars().all()
    )
    printers_q = (
        _with_joins(select(Row.printer_id, Printer.name, Printer.archived).select_from(Row))
        .where(*filters, Row.printer_id.isnot(None))
        .distinct()
        .order_by(Printer.name)
    )
    printers = [
        {"id": int(pid), "name": name, "archived": bool(archived)}
        for pid, name, archived in (await db.execute(printers_q)).all()
        if pid is not None
    ]
    materials = list(
        (
            await db.execute(
                select(Row.material)
                .where(*filters, Row.material.isnot(None), Row.material != "")
                .distinct()
                .order_by(Row.material)
            )
        )
        .scalars()
        .all()
    )
    brands = list(
        (
            await db.execute(
                select(Row.brand).where(*filters, Row.brand.isnot(None), Row.brand != "").distinct().order_by(Row.brand)
            )
        )
        .scalars()
        .all()
    )
    return {"statuses": statuses, "printers": printers, "materials": materials, "brands": brands}


async def spool_usage(db: AsyncSession, *, source_token: str | None, spool_id: int) -> list[Row]:
    """Every row recorded under this spool id in the current source, newest first.

    The caller applies ``spoolman_usage_history.belongs_to`` — which of them are
    this spool's and which an earlier holder's of the id.
    """
    result = await db.execute(
        select(Row)
        .where(Row.source_token == (source_token or ""), Row.spoolman_spool_id == spool_id)
        .order_by(Row.created_at.desc(), Row.id.desc())
    )
    return list(result.scalars().all())
