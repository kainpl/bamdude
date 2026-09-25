"""Scheduled AMS drying — one-shot runs and recurring rules (vault 60-specs/scheduled-drying-spec).

The routes only read the two tables; every write goes through
``services/scheduled_drying.py``, the tables' one writer.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.timezones import server_timezone
from backend.app.models.scheduled_drying import DryingSchedule, ScheduledDrying
from backend.app.models.user import User
from backend.app.schemas.scheduled_drying import (
    DryingScheduleCreate,
    DryingScheduleList,
    DryingScheduleResponse,
    DryingScheduleUpdate,
    ScheduledDryingCreate,
    ScheduledDryingResponse,
)
from backend.app.services import scheduled_drying as sd

router = APIRouter(tags=["scheduled-drying"])


def _refused(exc: sd.DryingRefused) -> HTTPException:
    return HTTPException(exc.status_code, str(exc))


@router.get("/scheduled-dryings", response_model=list[ScheduledDryingResponse])
async def list_runs(
    printer_id: int | None = None,
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
    db: AsyncSession = Depends(get_db),
):
    """Runs that wait or run, and failed / skipped ones until they are dismissed."""
    query = select(ScheduledDrying).where(ScheduledDrying.status.in_(sd.RUN_LISTED))
    if printer_id is not None:
        query = query.where(ScheduledDrying.printer_id == printer_id)
    query = query.order_by(ScheduledDrying.start_after.asc().nullsfirst(), ScheduledDrying.id.asc())
    return list((await db.execute(query)).scalars().all())


@router.post("/scheduled-dryings", response_model=ScheduledDryingResponse)
async def create_run(
    payload: ScheduledDryingCreate,
    user: User | None = RequirePermission(Permission.PRINTERS_CONTROL),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await sd.create_run(db, **payload.model_dump(), created_by_id=getattr(user, "id", None))
    except sd.DryingRefused as exc:
        raise _refused(exc) from exc


@router.delete("/scheduled-dryings/{run_id}")
async def cancel_run(
    run_id: int,
    _: User | None = RequirePermission(Permission.PRINTERS_CONTROL),
    db: AsyncSession = Depends(get_db),
):
    try:
        return {"status": await sd.cancel_run(db, run_id), "id": run_id}
    except sd.DryingRefused as exc:
        raise _refused(exc) from exc


@router.get("/drying-schedules", response_model=DryingScheduleList)
async def list_schedules(
    printer_id: int | None = None,
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
    db: AsyncSession = Depends(get_db),
):
    query = select(DryingSchedule)
    if printer_id is not None:
        query = query.where(DryingSchedule.printer_id == printer_id)
    rows = (await db.execute(query.order_by(DryingSchedule.printer_id, DryingSchedule.start_time))).scalars().all()
    return {"server_timezone": getattr(server_timezone(), "key", None) or "UTC", "schedules": list(rows)}


@router.post("/drying-schedules", response_model=DryingScheduleResponse)
async def create_schedule(
    payload: DryingScheduleCreate,
    user: User | None = RequirePermission(Permission.PRINTERS_CONTROL),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await sd.create_schedule(db, **payload.model_dump(), created_by_id=getattr(user, "id", None))
    except sd.DryingRefused as exc:
        raise _refused(exc) from exc


@router.patch("/drying-schedules/{schedule_id}", response_model=DryingScheduleResponse)
async def update_schedule(
    schedule_id: int,
    payload: DryingScheduleUpdate,
    _: User | None = RequirePermission(Permission.PRINTERS_CONTROL),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await sd.update_schedule(db, schedule_id, payload.model_dump(exclude_unset=True))
    except sd.DryingRefused as exc:
        raise _refused(exc) from exc


@router.delete("/drying-schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: int,
    _: User | None = RequirePermission(Permission.PRINTERS_CONTROL),
    db: AsyncSession = Depends(get_db),
):
    try:
        await sd.delete_schedule(db, schedule_id)
    except sd.DryingRefused as exc:
        raise _refused(exc) from exc
    return {"status": "deleted", "id": schedule_id}
