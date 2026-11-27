"""API routes for Home Assistant sensors bound to a storage location (#2824)."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.printer_scope import request_key_listed
from backend.app.core.websocket import ws_manager
from backend.app.models.ha_sensor_history import HASensorHistory
from backend.app.models.location import Location
from backend.app.models.location_ha_sensor import LocationHASensor
from backend.app.models.location_sensor_primary import LocationSensorPrimary
from backend.app.models.smart_sensor_binding import SmartSensorBinding
from backend.app.models.user import User
from backend.app.schemas.location_ha_sensor import (
    HADisplayEntity,
    LocationHASensorCreate,
    LocationHASensorManagementReading,
    LocationHASensorReading,
    LocationHASensorResponse,
    LocationHASensorUpdate,
)
from backend.app.services.ha_sensor_manager import utcnow_naive
from backend.app.services.homeassistant import homeassistant_service
from backend.app.services.location_ha_sensor_manager import location_ha_sensor_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/location-ha-sensors", tags=["location-ha-sensors"])

# Reuse the smart-plug permissions, same as ha_sensors.py: both surfaces are
# "the Home Assistant integration", just scoped to a location instead of a
# printer. INVENTORY_* would put HA entity bindings behind
# can_manage_inventory, which defaults to on for API keys (see auth.py) —
# an inventory-scoped key (e.g. a SpoolBuddy kiosk) would then be able to
# create, edit and delete HA sensor bindings, a capability the printer
# sibling deliberately keeps admin-only by leaving SMART_PLUGS_CREATE/
# UPDATE/DELETE off the API-key allowlist entirely.
_READ = RequirePermission(Permission.SMART_SENSORS_READ)
_CREATE = RequirePermission(Permission.SMART_SENSORS_CREATE)
_UPDATE = RequirePermission(Permission.SMART_SENSORS_UPDATE)
_DELETE = RequirePermission(Permission.SMART_SENSORS_DELETE)


class PrimarySelection(BaseModel):
    location_id: int
    category: Literal["temperature", "humidity", "battery"]
    source: Literal["ha", "zigbee"]
    binding_id: int


async def _primary_target_is_valid(db: AsyncSession, row: LocationSensorPrimary | PrimarySelection) -> bool:
    if row.source == "ha":
        sensor = await db.get(LocationHASensor, row.binding_id)
        return sensor is not None and sensor.location_id == row.location_id and sensor.device_class == row.category
    binding = await db.get(SmartSensorBinding, row.binding_id)
    return binding is not None and binding.storage_location_id == row.location_id


@router.get("/primary")
async def list_location_sensor_primary(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = _READ,
):
    _check_storage_scope(request)
    rows = (await db.scalars(select(LocationSensorPrimary))).all()
    return [
        {"location_id": row.location_id, "category": row.category, "source": row.source, "binding_id": row.binding_id}
        for row in rows
        if await _primary_target_is_valid(db, row)
    ]


@router.put("/primary")
async def set_location_sensor_primary(
    data: PrimarySelection,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = _UPDATE,
):
    _check_storage_scope(request)
    from backend.app.services.sensor_target_lock import lock_sensor_target

    if not await lock_sensor_target(db, "locations", data.location_id):
        raise HTTPException(404, "Location not found")
    if not await _primary_target_is_valid(db, data):
        raise HTTPException(422, "Sensor binding does not belong to this location and category")
    row = await db.get(LocationSensorPrimary, (data.location_id, data.category))
    if row is None:
        row = LocationSensorPrimary(
            location_id=data.location_id, category=data.category, source=data.source, binding_id=data.binding_id
        )
        db.add(row)
    else:
        row.source, row.binding_id = data.source, data.binding_id
    await db.commit()
    await ws_manager.broadcast({"type": "sensor_bindings_changed"})
    return data


# Mirrors categoryFor() in LocationHASensorModal.tsx, which also gates that
# dialog's entity picker. A device class outside these three has no category
# and is not subject to the one-per-location rule below.
#
# "moisture" is deliberately not mapped to humidity: it is Home Assistant's
# binary wet/dry class, so a leak detector would otherwise block a real
# hygrometer on the same location, and it could not carry the category's
# thresholds anyway — the schema rejects alert_above/alert_below for
# kind="binary".
_CATEGORY_BY_DEVICE_CLASS = {
    "temperature": "temperature",
    "humidity": "humidity",
    "battery": "battery",
}


def _category_for(device_class: str | None) -> str | None:
    return _CATEGORY_BY_DEVICE_CLASS.get(device_class) if device_class else None


def _check_storage_scope(request: Request) -> None:
    # A storage place holds no printer: a person limited to printers sees its sensors like the
    # spools (N62 r1 B06); a key with a printer list of its own reads no storage at all.
    if request_key_listed(request):
        raise HTTPException(403, "A printer-scoped API key cannot read storage sensor bindings")


async def _reject_duplicate_category(
    db: AsyncSession,
    location_id: int,
    device_class: str | None,
    exclude_sensor_id: int | None = None,
) -> None:
    """One sensor per category per location, enforced here and not only in the UI.

    The inventory column and the card footer both pick their reading with a
    single ``find`` over the location's sensors, so a second temperature
    sensor does not show up alongside the first — it silently shadows it
    depending on row order. The modal already prompts to replace rather than
    add, so this closes the same rule for direct API callers instead of
    leaving the guarantee resting on the client.
    """
    category = _category_for(device_class)
    if category is None:
        return

    query = select(LocationHASensor).where(LocationHASensor.location_id == location_id)
    if exclude_sensor_id is not None:
        query = query.where(LocationHASensor.id != exclude_sensor_id)

    result = await db.execute(query)
    for other in result.scalars().all():
        if _category_for(other.device_class) == category:
            raise HTTPException(
                400,
                f"This location already has a {category} sensor ({other.entity_id}). "
                "Edit that sensor to point at a different entity instead.",
            )


async def _refresh_quietly(sensor: LocationHASensor, db: AsyncSession) -> None:
    """Take a first reading without letting it fail the write that preceded it.

    The sensor row is committed before this runs. A failure here costs the
    card one poll interval of blank state, which is not worth turning a
    successful save into an error response.
    """
    try:
        await location_ha_sensor_manager.refresh_one(db, sensor)
    except Exception as e:
        logger.warning("Could not read %s right after saving it: %s", sensor.entity_id, e)


@router.get("/", response_model=list[LocationHASensorResponse])
async def list_location_ha_sensors(
    request: Request,
    location_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = _READ,
):
    """List configured sensors, grouped by location and in display order."""
    _check_storage_scope(request)
    query = select(LocationHASensor)
    if location_id is not None:
        query = query.where(LocationHASensor.location_id == location_id)
    result = await db.execute(query.order_by(LocationHASensor.location_id, LocationHASensor.sort_order))
    return list(result.scalars().all())


class LocationSensorManagementBatch(BaseModel):
    configured: bool
    readings: list[LocationHASensorManagementReading]


@router.get("/management/readings", response_model=LocationSensorManagementBatch)
async def list_location_sensor_management_readings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = _READ,
):
    """All storage bindings from the local poller cache, including hidden rows."""
    from backend.app.api.routes.settings import get_homeassistant_settings

    _check_storage_scope(request)
    settings = await get_homeassistant_settings(db)
    cadence = await location_ha_sensor_manager._get_poll_interval()
    rows = (
        await db.scalars(
            select(LocationHASensor).order_by(
                LocationHASensor.location_id, LocationHASensor.sort_order, LocationHASensor.id
            )
        )
    ).all()
    now = utcnow_naive()
    readings = []
    for sensor in rows:
        cached = location_ha_sensor_manager.get_reading(sensor.id)
        fresh = bool(cached and cached.observed_at and now - cached.observed_at <= timedelta(seconds=cadence * 2))
        readings.append(
            LocationHASensorManagementReading(
                id=sensor.id,
                location_id=sensor.location_id,
                name=sensor.name,
                entity_id=sensor.entity_id,
                kind=sensor.kind,
                device_class=sensor.device_class,
                unit=sensor.unit,
                state=cached.state if cached else sensor.last_state,
                value=cached.value if cached else None,
                alerting=cached.alerting if cached and fresh else False,
                reachable=bool(cached and cached.reachable and fresh),
                alert_state=sensor.alert_state,
                alert_above=sensor.alert_above,
                alert_below=sensor.alert_below,
                last_changed=sensor.last_changed,
                show_on_card=sensor.show_on_card,
                observed_at=cached.observed_at if cached else None,
                last_checked=sensor.last_checked,
                fresh=fresh,
            )
        )
    return LocationSensorManagementBatch(
        configured=bool(settings["ha_enabled"] and settings["ha_url"] and settings["ha_token"]),
        readings=readings,
    )


# Must precede /{sensor_id} so "entities" is not parsed as an id.
@router.get("/entities", response_model=list[HADisplayEntity])
async def list_bindable_entities(
    request: Request,
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = _READ,
):
    """List the Home Assistant entities that can be bound to a storage location."""
    _check_storage_scope(request)
    from backend.app.api.routes.settings import get_homeassistant_settings

    ha_settings = await get_homeassistant_settings(db)
    if not ha_settings["ha_enabled"] or not ha_settings["ha_url"] or not ha_settings["ha_token"]:
        raise HTTPException(
            400,
            "Home Assistant not configured. Please set HA URL and token in Settings → Network → Home Assistant.",
        )

    try:
        entities = await homeassistant_service.list_display_entities(
            ha_settings["ha_url"], ha_settings["ha_token"], search
        )
    except Exception as exc:
        raise HTTPException(502, "Home Assistant entity discovery is unavailable") from exc
    return [HADisplayEntity(**e) for e in entities]


@router.get("/by-location/{location_id}/readings", response_model=list[LocationHASensorReading])
async def get_location_sensor_readings(
    location_id: int,
    request: Request,
    show_on_card: bool = True,
    db: AsyncSession = Depends(get_db),
    _: User | None = _READ,
):
    """Live state of a location's card-visible sensors.

    Served from the poller's cache, so a page full of filament cards costs
    Home Assistant nothing. A sensor the poller has not reached yet falls
    back to its last persisted state, marked unreachable, rather than
    vanishing from the card on every restart.
    """
    _check_storage_scope(request)
    conditions = [LocationHASensor.location_id == location_id]
    if show_on_card:
        conditions.append(LocationHASensor.show_on_card.is_(True))

    result = await db.execute(
        select(LocationHASensor).where(*conditions).order_by(LocationHASensor.sort_order, LocationHASensor.id)
    )

    readings = []
    for sensor in result.scalars().all():
        cached = location_ha_sensor_manager.get_reading(sensor.id)
        readings.append(
            LocationHASensorReading(
                id=sensor.id,
                name=sensor.name,
                entity_id=sensor.entity_id,
                kind=sensor.kind,
                device_class=sensor.device_class,
                unit=sensor.unit,
                state=cached.state if cached else sensor.last_state,
                value=cached.value if cached else None,
                alerting=cached.alerting if cached else False,
                reachable=cached.reachable if cached else False,
                alert_state=sensor.alert_state,
                alert_above=sensor.alert_above,
                alert_below=sensor.alert_below,
                last_changed=sensor.last_changed,
                show_on_card=sensor.show_on_card,
            )
        )
    return readings


@router.post("/", response_model=LocationHASensorResponse)
async def create_location_ha_sensor(
    data: LocationHASensorCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = _CREATE,
):
    """Bind a Home Assistant entity to a storage location."""
    _check_storage_scope(request)
    location = await db.get(Location, data.location_id)
    if not location:
        raise HTTPException(404, "Location not found")
    from backend.app.services.sensor_target_lock import lock_sensor_target

    if not await lock_sensor_target(db, "locations", data.location_id):
        raise HTTPException(404, "Location not found")

    existing = await db.execute(
        select(LocationHASensor).where(
            LocationHASensor.location_id == data.location_id,
            LocationHASensor.entity_id == data.entity_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"{data.entity_id} is already bound to this location")

    await _reject_duplicate_category(db, data.location_id, data.device_class)

    sensor = LocationHASensor(**data.model_dump())
    db.add(sensor)
    try:
        await db.commit()
    except IntegrityError:
        # The duplicate check above is read-then-insert, so a concurrent
        # create for the same (location, entity) can get past it — the unique
        # index is the backstop, and its loser should read like the pre-check.
        await db.rollback()
        raise HTTPException(400, f"{data.entity_id} is already bound to this location") from None
    await db.refresh(sensor)
    logger.info("Bound HA entity %s to location %s as '%s'", sensor.entity_id, sensor.location_id, sensor.name)

    # Read it once now so the card shows a state immediately instead of after
    # the next poll tick. Best-effort: the row is already committed, so
    # letting a Home Assistant hiccup 500 the request would report a failure
    # for work that succeeded — and the retry would come back "already bound".
    await _refresh_quietly(sensor, db)
    await ws_manager.broadcast({"type": "sensor_bindings_changed"})
    return sensor


@router.get("/{sensor_id}", response_model=LocationHASensorResponse)
async def get_location_ha_sensor(
    sensor_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = _READ,
):
    sensor = await db.get(LocationHASensor, sensor_id)
    if not sensor:
        raise HTTPException(404, "Sensor not found")
    _check_storage_scope(request)
    return sensor


@router.get("/{sensor_id}/history")
async def get_location_ha_sensor_history(
    sensor_id: int,
    request: Request,
    hours: int = Query(24, ge=1, le=720),
    db: AsyncSession = Depends(get_db),
    _: User | None = _READ,
):
    _check_storage_scope(request)
    sensor = await db.get(LocationHASensor, sensor_id)
    if not sensor:
        raise HTTPException(404, "Sensor not found")
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    rows = (
        await db.scalars(
            select(HASensorHistory)
            .where(
                HASensorHistory.location_sensor_id == sensor_id,
                HASensorHistory.observed_at >= since,
            )
            .order_by(HASensorHistory.observed_at.desc(), HASensorHistory.id.desc())
            .limit(2000)
        )
    ).all()
    return [
        {
            "id": row.id,
            "revision": row.revision,
            "entity_id": row.entity_id,
            "kind": row.kind,
            "unit": row.unit,
            "state": row.state,
            "value": row.value,
            "observed_at": row.observed_at,
        }
        for row in reversed(rows)
    ]


@router.patch("/{sensor_id}", response_model=LocationHASensorResponse)
async def update_location_ha_sensor(
    sensor_id: int,
    data: LocationHASensorUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = _UPDATE,
):
    sensor = await db.get(LocationHASensor, sensor_id)
    if not sensor:
        raise HTTPException(404, "Sensor not found")
    _check_storage_scope(request)
    from backend.app.services.sensor_target_lock import lock_sensor_target

    if not await lock_sensor_target(db, "locations", sensor.location_id):
        raise HTTPException(404, "Location not found")

    updates = data.model_dump(exclude_unset=True)

    # Re-run the create-time rules against the merged row. A PATCH that only
    # sets show_on_card has no entity_id or alert_state in its payload, so the
    # schema alone cannot tell whether the result is coherent.
    merged = {field: getattr(sensor, field) for field in LocationHASensorCreate.model_fields}
    merged.update(updates)
    try:
        LocationHASensorCreate(**merged)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e

    # Same uniqueness rule as create: repointing a sensor at an entity the
    # location already has would leave two rows fighting over one reading.
    new_entity = updates.get("entity_id")
    if new_entity and new_entity != sensor.entity_id:
        clash = await db.execute(
            select(LocationHASensor).where(
                LocationHASensor.location_id == sensor.location_id,
                LocationHASensor.entity_id == new_entity,
                LocationHASensor.id != sensor.id,
            )
        )
        if clash.scalar_one_or_none():
            raise HTTPException(400, f"{new_entity} is already bound to this location")

    # Same one-per-category rule as create, against the merged row and
    # excluding this sensor — repointing a sensor within its own category
    # (the modal's replace flow) stays allowed.
    if "device_class" in updates:
        await _reject_duplicate_category(db, sensor.location_id, merged["device_class"], exclude_sensor_id=sensor.id)
        if updates["device_class"] != sensor.device_class:
            await db.execute(
                delete(LocationSensorPrimary).where(
                    LocationSensorPrimary.source == "ha", LocationSensorPrimary.binding_id == sensor.id
                )
            )

    for field, value in updates.items():
        if field in {"entity_id", "kind", "unit"} and value != getattr(sensor, field):
            sensor.history_revision += 1
            sensor.last_state = None
            sensor.last_changed = None
            break
    for field, value in updates.items():
        setattr(sensor, field, value)
    # Read before commit: after a rollback the instance is expired, and
    # touching its attributes from async code raises MissingGreenlet.
    entity_id = sensor.entity_id
    try:
        await db.commit()
    except IntegrityError:
        # Same backstop as create: the clash check above races a concurrent
        # write, and the unique index decides who loses.
        await db.rollback()
        raise HTTPException(400, f"{entity_id} is already bound to this location") from None
    await db.refresh(sensor)

    # The entity or its alert rule may have changed under the cached reading.
    await _refresh_quietly(sensor, db)
    await ws_manager.broadcast({"type": "sensor_bindings_changed"})
    return sensor


@router.delete("/{sensor_id}")
async def delete_location_ha_sensor(
    sensor_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = _DELETE,
):
    sensor = await db.get(LocationHASensor, sensor_id)
    if not sensor:
        raise HTTPException(404, "Sensor not found")
    _check_storage_scope(request)

    name = sensor.name
    await db.execute(delete(HASensorHistory).where(HASensorHistory.location_sensor_id == sensor_id))
    await db.execute(
        delete(LocationSensorPrimary).where(
            LocationSensorPrimary.source == "ha", LocationSensorPrimary.binding_id == sensor.id
        )
    )
    await db.delete(sensor)
    await db.commit()
    location_ha_sensor_manager.forget(sensor_id)
    await ws_manager.broadcast({"type": "sensor_bindings_changed"})
    logger.info("Removed location HA sensor '%s'", name)
    return {"message": f"Sensor '{name}' removed"}
