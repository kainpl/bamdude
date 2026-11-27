"""Usage history of Spoolman spools — the History view and the spool dialog's
usage list in Spoolman mode (m228, upstream 1d86cff5a).

Shaped like the built-in inventory's ``/inventory/usage`` endpoints so the same
frontend components read both, but a separate door: the rows live in their own
table, are fenced to the current Spoolman (``spoolman_usage_history.current_token``),
and every route here refuses while Spoolman mode is off — the built-in
inventory never reads them.

Clearing or deleting history is bookkeeping only: nothing here writes to
Spoolman, and a spool's weight there is not changed by it (in the built-in
inventory a deleted row returns its weight to the spool; Spoolman owns that
figure here).
"""

from __future__ import annotations

import math
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.spoolman_inventory import _get_client, _translate_spoolman_errors
from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.websocket import ws_manager
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.schemas.archive import PaginationMeta
from backend.app.schemas.spool_usage import (
    SpoolmanUsageSourceState,
    SpoolUsageFacetPrinter,
    SpoolUsageFacets,
    SpoolUsageHistoryResponse,
    SpoolUsageListItem,
    SpoolUsagePage,
    SpoolUsageSpoolRef,
    SpoolUsageTotals,
)
from backend.app.services import spoolman_source_changes, spoolman_usage_history, spoolman_usage_service

router = APIRouter(prefix="/spoolman/inventory", tags=["spoolman-inventory"])

_USAGE_PRINTER_ID_PATTERN = r"^(__none__|\d+)$"


async def _require_spoolman_mode(db: AsyncSession) -> None:
    """These rows belong to the Spoolman inventory; the built-in one never reads them."""
    raw = (await db.execute(select(Settings.value).where(Settings.key == "spoolman_enabled"))).scalar_one_or_none()
    if (raw or "").lower() != "true":
        raise HTTPException(status_code=400, detail="Spoolman integration is not enabled")


async def _registered_of(db: AsyncSession, spool_id: int) -> str | None:
    """Spoolman's ``registered`` stamp of the spool holding this id now."""
    client = await _get_client(db)
    async with _translate_spoolman_errors():
        spool = await client.get_spool(spool_id)
    registered = spool.get("registered") if isinstance(spool, dict) else None
    return str(registered) if registered else None


def _usage_scope(request: Request) -> list:
    """N62: usage rows of the printers the caller reaches, and those of no printer."""
    from backend.app.core.printer_scope import request_printer_scope, where as printer_scope_where
    from backend.app.models.spoolman_usage_history import SpoolmanUsageHistory

    clause = printer_scope_where(request_printer_scope(request), SpoolmanUsageHistory.printer_id)
    return [] if clause is None else [clause]


@router.get("/usage/facets", response_model=SpoolUsageFacets)
async def get_spoolman_usage_facets(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """The History view's filter options, over every row of the current Spoolman."""
    await _require_spoolman_mode(db)
    token = await spoolman_usage_history.current_token(db)
    facets = await spoolman_usage_service.usage_facets(
        db, filters=[*spoolman_usage_service.build_filters(source_token=token), *_usage_scope(request)]
    )
    return SpoolUsageFacets(
        statuses=facets["statuses"],
        printers=[SpoolUsageFacetPrinter(**row) for row in facets["printers"]],
        materials=facets["materials"],
        brands=facets["brands"],
    )


@router.get("/usage/source", response_model=SpoolmanUsageSourceState)
async def get_spoolman_usage_source(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """Whether usage history of a previous Spoolman address is being kept apart."""
    await _require_spoolman_mode(db)
    return SpoolmanUsageSourceState(**await spoolman_usage_history.source_state(db))


@router.post("/usage/source/keep-previous", response_model=SpoolmanUsageSourceState)
async def keep_previous_spoolman_usage_source(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """The address changed but it is the SAME Spoolman database: its history comes along.

    A change of the source (C047 §C1): under the source guard, refused only while a
    print is being charged (``keep_previous`` names the same database, so it is no
    change of the base); the lazy transition and the move commit together.
    """
    await _require_spoolman_mode(db)
    async with spoolman_source_changes.owning(db) as change:
        await change.decide(db, record_changes=True)
        await spoolman_usage_history.keep_previous_source(db)
    await ws_manager.broadcast({"type": "inventory_changed"})
    return SpoolmanUsageSourceState(**await spoolman_usage_history.source_state(db))


@router.post("/usage/source/rotate", response_model=SpoolmanUsageSourceState)
async def rotate_spoolman_usage_source(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """A NEW Spoolman database behind the same address: usage history starts over.

    A change of the BASE (C047 §C1, S38): refused while any print accounts in
    Spoolman — the numbers it froze belong to the database being left.
    """
    await _require_spoolman_mode(db)
    async with spoolman_source_changes.owning(db) as change:
        await change.decide(db, rotate=True)
        await spoolman_usage_history.rotate_source(db)
    await ws_manager.broadcast({"type": "inventory_changed"})
    return SpoolmanUsageSourceState(**await spoolman_usage_history.source_state(db))


@router.get("/usage", response_model=SpoolUsagePage)
async def get_spoolman_usage_history(
    request: Request,
    printer_id: str | None = Query(
        None,
        pattern=_USAGE_PRINTER_ID_PATTERN,
        description="Printer id, or '__none__' for rows recorded against no printer",
    ),
    spool_id: int | None = Query(None, description="A Spoolman spool id"),
    status: list[str] = Query(default_factory=list, description="Repeatable — any of them matches"),
    material: str | None = Query(None),
    brand: str | None = Query(None),
    assigned: str | None = Query(
        None,
        pattern="^(assigned|unassigned)$",
        description="Whether the spool sits in a printer now. Omitted means both",
    ),
    date_from: datetime | None = Query(None, description="Inclusive, an absolute instant (UTC)"),
    date_to: datetime | None = Query(None, description="EXCLUSIVE, an absolute instant (UTC)"),
    q: str | None = Query(None, description="Tokenised match over print name, spool and printer"),
    sort_by: str | None = Query(None, description="<column>_asc|_desc; omitted keeps newest-first"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    all: bool = Query(False, description="Skip pagination and return every matching row"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
) -> SpoolUsagePage:
    """One server-filtered, server-sorted page of the current Spoolman's usage rows.

    ``totals`` covers the whole filter, not the page. Each item's ``spool`` is
    the snapshot taken when the charge landed — no Spoolman round trip.
    """
    await _require_spoolman_mode(db)
    filters = spoolman_usage_service.build_filters(
        source_token=await spoolman_usage_history.current_token(db),
        statuses=status or None,
        printer_id=printer_id,
        spool_id=spool_id,
        material=material,
        brand=brand,
        assigned=assigned,
        date_from=date_from,
        date_to=date_to,
        q=q,
    )
    filters = [*filters, *_usage_scope(request)]
    offset, page_limit = (0, None) if all else ((page - 1) * per_page, per_page)

    total = await spoolman_usage_service.count_usage(db, filters=filters)
    rows = await spoolman_usage_service.list_usage(
        db, filters=filters, sort_by=sort_by, limit=page_limit, offset=offset
    )
    totals = await spoolman_usage_service.usage_totals(db, filters=filters)

    return SpoolUsagePage(
        items=[
            SpoolUsageListItem(
                id=row.id,
                spool_id=row.spoolman_spool_id,
                created_at=row.created_at,
                weight_used=row.weight_used,
                percent_used=row.percent_used,
                status=row.status,
                cost=row.cost,
                print_name=row.print_name,
                archive_id=row.archive_id,
                printer_id=row.printer_id,
                printer_name=printer.name if printer else None,
                printer_archived=bool(printer and printer.archived),
                spool=SpoolUsageSpoolRef(
                    id=row.spoolman_spool_id,
                    material=row.material,
                    subtype=row.subtype,
                    brand=row.brand,
                    color_name=row.color_name,
                    rgba=row.rgba,
                    slicer_filament_name=row.slicer_filament_name,
                    label_weight=row.label_weight,
                ),
            )
            for row, printer in rows
        ],
        meta=PaginationMeta(
            total=total,
            current_page=1 if all else page,
            per_page=(total or 1) if all else per_page,
            last_page=1 if all else max(1, math.ceil(total / per_page)),
        ),
        totals=SpoolUsageTotals(weight_used=totals["weight_used"], cost=totals["cost"]),
    )


@router.get("/spools/{spool_id}/usage", response_model=list[SpoolUsageHistoryResponse])
async def get_spoolman_spool_usage(
    request: Request,
    spool_id: int = Path(..., gt=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """A Spoolman spool's prints, as BamDude charged them.

    Only THIS spool's: rows an earlier holder of the id left behind are not
    its own (``spoolman_usage_history.belongs_to``).
    """
    await _require_spoolman_mode(db)
    registered = await _registered_of(db, spool_id)
    rows = await spoolman_usage_service.spool_usage(
        db, source_token=await spoolman_usage_history.current_token(db), spool_id=spool_id
    )
    from backend.app.core.printer_scope import in_request_scope

    own = [
        r
        for r in rows
        if spoolman_usage_history.belongs_to(r.spool_registered, r.created_at, registered)
        and in_request_scope(request, r.printer_id)
    ]
    return [
        SpoolUsageHistoryResponse(
            id=r.id,
            spool_id=r.spoolman_spool_id,
            printer_id=r.printer_id,
            print_name=r.print_name,
            weight_used=r.weight_used,
            percent_used=r.percent_used,
            status=r.status,
            cost=r.cost,
            created_at=r.created_at,
        )
        for r in own[:limit]
    ]


@router.delete("/spools/{spool_id}/usage")
async def clear_spoolman_spool_usage(
    spool_id: int = Path(..., gt=0),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
) -> dict:
    """Forget a Spoolman spool's usage history. Spoolman's own totals are untouched."""
    await _require_spoolman_mode(db)
    registered = await _registered_of(db, spool_id)
    deleted = await spoolman_usage_history.clear_for_spool(db, spool_id, registered)
    await db.commit()
    await ws_manager.broadcast({"type": "inventory_changed"})
    return {"status": "cleared", "deleted": deleted}


@router.delete("/spools/{spool_id}/usage/{usage_id}")
async def delete_spoolman_spool_usage_record(
    spool_id: int = Path(..., gt=0),
    usage_id: int = Path(..., gt=0),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
) -> dict:
    """Forget one usage row. Spoolman's own totals are untouched."""
    await _require_spoolman_mode(db)
    if not await spoolman_usage_history.delete_row(db, spool_id, usage_id):
        raise HTTPException(404, "Usage record not found")
    await db.commit()
    await ws_manager.broadcast({"type": "inventory_changed"})
    return {"status": "deleted"}
