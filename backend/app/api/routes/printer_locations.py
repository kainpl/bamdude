"""Places on the farm: list, create, rename, delete.

Deleting is blocked while anything points at a place rather than nulling the
references. The queued items are the reason: an item waiting for a specific
place would otherwise start routing anywhere — a change of behaviour nobody
asked for and nothing announces.

No new permissions. Managing the list of places is farm configuration, not
creating or deleting printers, so reads ride ``PRINTERS_READ`` and writes ride
``PRINTERS_UPDATE``.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_location import PrinterLocation
from backend.app.models.smart_sensor import SmartSensor
from backend.app.models.user import User
from backend.app.schemas.printer_location import (
    PrinterLocationCreate,
    PrinterLocationListItem,
    PrinterLocationOut,
    PrinterLocationUpdate,
)
from backend.app.services.printer_location_service import location_key, normalize_location

router = APIRouter(prefix="/printer-locations", tags=["printer-locations"])

_NAME_TAKEN = "A location with this name already exists."


async def _holders(db, location_id: int) -> tuple[int, int, int]:
    """How many printers, sensors and queued items point at this place."""
    counts = []
    for model, column in (
        (Printer, Printer.location_id),
        (SmartSensor, SmartSensor.location_id),
        (AutoQueueItem, AutoQueueItem.target_location_id),
    ):
        counts.append(
            (await db.execute(select(func.count()).select_from(model).where(column == location_id))).scalar_one()
        )
    return counts[0], counts[1], counts[2]


async def _name_is_taken(db, name: str, exclude_id: int | None = None) -> bool:
    """Case-insensitively, which is the whole point of the entity."""
    query = select(PrinterLocation.id).where(PrinterLocation.name_key == location_key(name))
    if exclude_id is not None:
        query = query.where(PrinterLocation.id != exclude_id)
    return (await db.execute(query)).scalar_one_or_none() is not None


@router.get("")
async def list_locations(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
):
    """Every place, with how many things hold it — the manager needs both."""
    rows = (await db.execute(select(PrinterLocation).order_by(PrinterLocation.name))).scalars().all()
    locations = []
    for row in rows:
        printers, sensors, queued = await _holders(db, row.id)
        locations.append(
            PrinterLocationListItem(
                id=row.id,
                name=row.name,
                printer_count=printers,
                sensor_count=sensors,
                queued_count=queued,
            )
        )
    return {"locations": locations}


@router.post("", status_code=201, response_model=PrinterLocationOut)
async def create_location(
    payload: PrinterLocationCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
):
    name = normalize_location(payload.name)
    if await _name_is_taken(db, name):
        raise HTTPException(status_code=409, detail=_NAME_TAKEN)
    row = PrinterLocation(name=name, name_key=location_key(name))
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.patch("/{location_id}", response_model=PrinterLocationOut)
async def rename_location(
    location_id: int,
    payload: PrinterLocationUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
):
    """Rename in place — every reference is by id, so it lands everywhere.

    Including the auto-queue items already waiting for that place, which under
    the old string scheme would have quietly stopped matching.
    """
    row = await db.get(PrinterLocation, location_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such location.")
    name = normalize_location(payload.name)
    if await _name_is_taken(db, name, exclude_id=location_id):
        raise HTTPException(status_code=409, detail=_NAME_TAKEN)
    row.name = name
    # Rewritten with the name, always. A new name carrying the old key is unique
    # while its lookup still matches the old spelling, so the next
    # differently-cased duplicate would be accepted — the entity would then hold
    # the very problem it exists to remove.
    row.name_key = location_key(name)
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/{location_id}")
async def delete_location(
    location_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
):
    row = await db.get(PrinterLocation, location_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such location.")
    printers, sensors, queued = await _holders(db, location_id)
    if printers or sensors or queued:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This location is still in use: {printers} printer(s), {sensors} sensor(s), "
                f"{queued} queued item(s). Move them first."
            ),
        )
    await db.delete(row)
    await db.commit()
    return {"deleted": location_id}
