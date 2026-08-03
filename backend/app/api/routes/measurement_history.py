"""Reading a window of measurement history back.

The same shape and bounds as ``ams-history``, so a reader who knows one knows
the other. The window reaches a week while retention keeps a month, and the gap
is deliberate: thirty days of five-second readings is half a million points for
one plug, useful only aggregated — and aggregation is not part of this stage.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
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

# How wide a bucket is at a given window. A table rather than a formula so every
# bucket is a round number that can be named in words -- "5-minute average" --
# and so the point count stays near 300 whatever the window.
#
# The last row exists because the window is capped at _MAX_HOURS; the two have
# to move together.
_BUCKET_SECONDS: tuple[tuple[int, int], ...] = (
    (6, 60),
    (24, 300),
    (48, 600),
    (_MAX_HOURS, 1800),
)


def bucket_seconds_for(hours: int) -> int:
    """The bucket width for a window, from the table."""
    for limit, seconds in _BUCKET_SECONDS:
        if hours <= limit:
            return seconds
    return _BUCKET_SECONDS[-1][1]


@router.get("/smart-plugs/{plug_id}/power-history")
async def get_plug_power_history(
    plug_id: int,
    hours: int = Query(default=24, ge=1, le=_MAX_HOURS, description="Hours of history (1-168)"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SMART_PLUGS_READ),
):
    """What this plug was drawing, oldest point first.

    Buckets, not readings. One plug produces about 58 readings every five
    minutes -- some 17 000 a day -- so a 24-hour window of raw points is far
    more than a chart can draw or a browser should be handed. Each point is the
    average over ``bucket_seconds``, which rides along so the reader can say so
    rather than presenting an average as an instant.
    """
    if await db.get(SmartPlug, plug_id) is None:
        raise HTTPException(status_code=404, detail="No such plug.")

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    bucket = bucket_seconds_for(hours)
    window = (SmartPlugPowerHistory.plug_id == plug_id, SmartPlugPowerHistory.recorded_at >= since)

    # One expression for both engines: SQLAlchemy renders extract('epoch') as
    # STRFTIME('%s', ...) on SQLite and EXTRACT(epoch FROM ...) on PostgreSQL,
    # so there is no dialect branch to keep in sync.
    bucket_index = func.floor(func.extract("epoch", SmartPlugPowerHistory.recorded_at) / bucket)

    rows = (
        await db.execute(
            select(bucket_index.label("bucket"), func.avg(SmartPlugPowerHistory.power).label("power"))
            .where(*window)
            .group_by(bucket_index)
            .order_by(bucket_index)
        )
    ).all()

    # The statistics are taken over the READINGS, not the buckets: averaging
    # first would lose the peak, which is the one number the smoothed line
    # cannot show.
    stats = (
        await db.execute(
            select(
                func.min(SmartPlugPowerHistory.power),
                func.avg(SmartPlugPowerHistory.power),
                func.max(SmartPlugPowerHistory.power),
            ).where(*window)
        )
    ).one()

    # int(): SQLite returns the bucket index as a float, and range() would not
    # take it.
    measured = {int(row.bucket): float(row.power) for row in rows if row.power is not None}
    points = []
    if measured:
        # From the first reading to the last, so a gap inside the span shows as
        # one -- and a plug added this morning still answers with an empty list
        # rather than a window full of nulls.
        for index in range(min(measured), max(measured) + 1):
            value = measured.get(index)
            points.append(
                {
                    "recorded_at": datetime.fromtimestamp(index * bucket, tz=timezone.utc).isoformat(),
                    "power": round(value, 1) if value is not None else None,
                }
            )

    return {
        "points": points,
        "bucket_seconds": bucket,
        "min_power": round(stats[0], 1) if stats[0] is not None else None,
        "avg_power": round(stats[1], 1) if stats[1] is not None else None,
        "max_power": round(stats[2], 1) if stats[2] is not None else None,
    }


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
