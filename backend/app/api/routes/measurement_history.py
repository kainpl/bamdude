"""Reading a window of measurement history back.

The same shape and bounds as ``ams-history``, so a reader who knows one knows
the other. The window reaches a week while retention keeps a month, and the gap
is deliberate: thirty days of five-second readings is half a million points for
one plug, useful only aggregated — and aggregation is not part of this stage.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.smart_plug import SmartPlug
from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory
from backend.app.models.smart_sensor import SmartSensor
from backend.app.models.smart_sensor_history import SmartSensorHistory
from backend.app.models.user import User

router = APIRouter(tags=["measurement-history"])

_MAX_HOURS = 168  # a week


@router.get("/smart-plugs/{plug_id}/power-history")
async def get_plug_power_history(
    plug_id: int,
    hours: int = Query(default=24, ge=1, le=_MAX_HOURS, description="Hours of history (1-168)"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SMART_PLUGS_READ),
):
    """What this plug was drawing, oldest point first."""
    if await db.get(SmartPlug, plug_id) is None:
        raise HTTPException(status_code=404, detail="No such plug.")

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (
        (
            await db.execute(
                select(SmartPlugPowerHistory)
                .where(SmartPlugPowerHistory.plug_id == plug_id, SmartPlugPowerHistory.recorded_at >= since)
                # Oldest first: a chart draws them in order, and an unordered
                # series zig-zags across the plot in a way that reads as bad
                # data rather than as a bad query.
                .order_by(SmartPlugPowerHistory.recorded_at)
            )
        )
        .scalars()
        .all()
    )

    return {"points": [{"recorded_at": row.recorded_at.isoformat(), "power": row.power} for row in rows]}


@router.get("/zigbee/sensors/{sensor_id}/history")
async def get_sensor_history(
    sensor_id: int,
    kind: str = Query(description="Which quantity — temperature, humidity, battery, …"),
    hours: int = Query(default=24, ge=1, le=_MAX_HOURS, description="Hours of history (1-168)"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SMART_SENSORS_READ),
):
    """One quantity at a time — they have different units and ranges, and a
    single series is what a chart draws."""
    if await db.get(SmartSensor, sensor_id) is None:
        raise HTTPException(status_code=404, detail="No such sensor.")

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (
        (
            await db.execute(
                select(SmartSensorHistory)
                .where(
                    SmartSensorHistory.sensor_id == sensor_id,
                    SmartSensorHistory.sensor_kind == kind,
                    SmartSensorHistory.recorded_at >= since,
                )
                .order_by(SmartSensorHistory.recorded_at)
            )
        )
        .scalars()
        .all()
    )

    return {"points": [{"recorded_at": row.recorded_at.isoformat(), "value": row.value} for row in rows]}
