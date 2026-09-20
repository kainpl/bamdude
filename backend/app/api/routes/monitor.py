"""Authenticated and scoped TV feeds; no printer-control side effects."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.auth import require_ownership_permission, require_permission, security
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.schemas.farm_forecast import FarmForecastOut
from backend.app.schemas.monitor import MonitorSnapshot, MonitorView
from backend.app.services import farm_forecast
from backend.app.services.long_lived_tokens import verify_token
from backend.app.services.monitor_snapshot import MonitorAccess, build_snapshot

router = APIRouter(prefix="/monitor", tags=["monitor"])
_printers_read = require_permission(Permission.PRINTERS_READ)
_queues_read = require_permission(Permission.QUEUE_READ)
_items_read = require_ownership_permission(Permission.QUEUE_READ_ALL, Permission.QUEUE_READ_OWN)


async def monitor_access(
    view: MonitorView = "printers",
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> MonitorAccess:
    user = await _printers_read(credentials=credentials, x_api_key=x_api_key)
    queue_read = False
    read_all = read_own = False
    try:
        await _queues_read(credentials=credentials, x_api_key=x_api_key)
        queue_read = True
    except HTTPException as exc:
        if exc.status_code != 403 or view == "queues":
            raise
    if queue_read:
        try:
            _, read_all = await _items_read(credentials=credentials, x_api_key=x_api_key)
            read_own = not read_all
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
    return MonitorAccess(
        queue_read=queue_read,
        read_all=read_all,
        read_own=read_own,
        user_id=user.id if user else None,
        open_printer=bool(user and user.has_permission(Permission.PRINTERS_READ.value)),
        open_queue=bool(user and queue_read),
    )


async def kiosk_access(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> MonitorAccess:
    record = await verify_token(db, credentials.credentials, scope="monitor") if credentials else None
    if record is None:
        raise HTTPException(401, detail="monitor_token_invalid")
    owner = await db.scalar(select(User).where(User.id == record.user_id).options(selectinload(User.groups)))
    if (
        owner is None
        or not owner.is_active
        or not owner.has_all_permissions(
            Permission.PRINTERS_READ.value,
            Permission.QUEUE_READ.value,
        )
    ):
        raise HTTPException(401, detail="monitor_token_invalid")
    return MonitorAccess(queue_read=True, kiosk=True)


def private_response(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


@router.get("/snapshot", response_model=MonitorSnapshot)
async def snapshot(
    response: Response,
    view: MonitorView = "printers",
    access: MonitorAccess = Depends(monitor_access),
    db: AsyncSession = Depends(get_db),
):
    private_response(response)
    return await build_snapshot(db, view, access)


@router.get("/kiosk/snapshot", response_model=MonitorSnapshot)
async def kiosk_snapshot(
    response: Response,
    view: MonitorView = "printers",
    access: MonitorAccess = Depends(kiosk_access),
    db: AsyncSession = Depends(get_db),
):
    private_response(response)
    return await build_snapshot(db, view, access)


@router.get("/kiosk/forecast", response_model=FarmForecastOut)
async def kiosk_forecast(
    response: Response, _: MonitorAccess = Depends(kiosk_access), db: AsyncSession = Depends(get_db)
):
    private_response(response)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return FarmForecastOut.of(now, farm_forecast.simulate_farm(await farm_forecast.load_snapshot(db, now)))
