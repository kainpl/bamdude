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
from backend.app.services.printer_location_service import (
    MAX_DEPTH,
    depth_of,
    load_tree,
    location_key,
    normalize_location,
    path_of,
    would_cycle,
)

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


async def _name_is_taken(db, name: str, parent_id: int | None, exclude_id: int | None = None) -> bool:
    """Case-insensitively, within one parent.

    The composite index cannot do this alone: on SQLite NULL != NULL, so two
    roots sharing a name pass straight through it. This check is the guard; the
    index is a backstop.
    """
    query = select(PrinterLocation.id).where(
        PrinterLocation.name_key == location_key(name),
        PrinterLocation.parent_id.is_(None) if parent_id is None else PrinterLocation.parent_id == parent_id,
    )
    if exclude_id is not None:
        query = query.where(PrinterLocation.id != exclude_id)
    return (await db.execute(query)).scalar_one_or_none() is not None


async def _check_placement(db, location_id: int | None, parent_id: int | None) -> None:
    """Refuse a parent that would make a ring or a fourth level."""
    if parent_id is None:
        return
    tree = await load_tree(db)
    if parent_id not in tree:
        raise HTTPException(status_code=422, detail="No such parent location.")
    if location_id is not None and would_cycle(tree, location_id, parent_id):
        raise HTTPException(status_code=422, detail="A location cannot be placed inside itself.")
    if depth_of(tree, parent_id) >= MAX_DEPTH:
        raise HTTPException(
            status_code=422,
            detail=f"Locations go {MAX_DEPTH} levels deep. This one would be deeper.",
        )


@router.get("")
async def list_locations(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
):
    """Every place, with how many things hold it — the manager needs both."""
    rows = (await db.execute(select(PrinterLocation))).scalars().all()
    tree = await load_tree(db)
    locations = []
    for row in rows:
        printers, sensors, queued = await _holders(db, row.id)
        locations.append(
            PrinterLocationListItem(
                id=row.id,
                name=row.name,
                parent_id=row.parent_id,
                path=path_of(tree, row.id),
                depth=depth_of(tree, row.id),
                printer_count=printers,
                sensor_count=sensors,
                queued_count=queued,
            )
        )
    # By path, so a parent leads its own children and the interface needs no
    # second rule for which group comes first.
    locations.sort(key=lambda item: item.path)
    return {"locations": locations}


@router.post("", status_code=201, response_model=PrinterLocationOut)
async def create_location(
    payload: PrinterLocationCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
):
    name = normalize_location(payload.name)
    await _check_placement(db, None, payload.parent_id)
    if await _name_is_taken(db, name, payload.parent_id):
        raise HTTPException(status_code=409, detail=_NAME_TAKEN)
    row = PrinterLocation(name=name, name_key=location_key(name), parent_id=payload.parent_id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return PrinterLocationOut.from_location(row)


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

    # "Not sent" is not "sent as null": a rename must not move the location, and
    # moving one back to the top level has to be sayable.
    moving = "parent_id" in payload.model_fields_set
    parent_id = payload.parent_id if moving else row.parent_id
    if moving:
        await _check_placement(db, location_id, payload.parent_id)

    name = normalize_location(payload.name) if payload.name is not None else row.name
    if await _name_is_taken(db, name, parent_id, exclude_id=location_id):
        raise HTTPException(status_code=409, detail=_NAME_TAKEN)

    row.name = name
    # Rewritten with the name, always. A new name carrying the old key is unique
    # while its lookup still matches the old spelling, so the next
    # differently-cased duplicate would be accepted — the entity would then hold
    # the very problem it exists to remove.
    row.name_key = location_key(name)
    if moving:
        row.parent_id = payload.parent_id
    await db.commit()
    await db.refresh(row)
    return PrinterLocationOut.from_location(row)


@router.delete("/{location_id}")
async def delete_location(
    location_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
):
    row = await db.get(PrinterLocation, location_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such location.")
    children = (
        await db.execute(
            select(func.count()).select_from(PrinterLocation).where(PrinterLocation.parent_id == location_id)
        )
    ).scalar_one()
    if children:
        raise HTTPException(
            status_code=409,
            detail=f"This location holds {children} other location(s). Remove or move them first.",
        )
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
