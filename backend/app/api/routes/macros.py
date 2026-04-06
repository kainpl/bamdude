"""API routes for macro management."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.macro import Macro
from backend.app.models.user import User
from backend.app.schemas.macro import MacroCreate, MacroResponse, MacroUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/macros", tags=["macros"])

# Available macro events
MACRO_EVENTS = {
    "swap_mode_start": "Swap Mode — Start Sequence",
    "swap_mode_change_table": "Swap Mode — Change Table",
}


@router.get("/meta")
async def get_macro_meta():
    """Get metadata for macro UI: available events and printer models."""
    from backend.app.utils.printer_model_names import PRINTER_MODEL_DISPLAY_NAMES

    return {
        "events": MACRO_EVENTS,
        "printer_models": PRINTER_MODEL_DISPLAY_NAMES,
    }


@router.get("/", response_model=list[MacroResponse])
async def list_macros(
    printer_model: str | None = Query(None, description="Filter by printer model"),
    swap_mode: bool | None = Query(None, description="Filter by swap_mode_only"),
    event: str | None = Query(None, description="Filter by event"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    """List all macros with optional filters."""
    query = select(Macro).order_by(Macro.event, Macro.name)

    if swap_mode is not None:
        if swap_mode:
            query = query.where(Macro.swap_mode_only == True)  # noqa: E712
    if event:
        query = query.where(Macro.event == event)

    result = await db.execute(query)
    macros = list(result.scalars().all())

    # Filter by printer_model in Python (JSON array in SQLite)
    if printer_model:
        macros = [
            m for m in macros
            if "*" in json.loads(m.printer_models) or printer_model in json.loads(m.printer_models)
        ]

    return macros


@router.get("/{macro_id}", response_model=MacroResponse)
async def get_macro(
    macro_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    """Get a single macro."""
    result = await db.execute(select(Macro).where(Macro.id == macro_id))
    macro = result.scalar_one_or_none()
    if not macro:
        raise HTTPException(404, "Macro not found")
    return macro


@router.post("/", response_model=MacroResponse)
async def create_macro(
    data: MacroCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    """Create a custom macro."""
    macro = Macro(
        name=data.name,
        printer_models=json.dumps(data.printer_models),
        swap_mode_only=data.swap_mode_only,
        event=data.event,
        gcode=data.gcode,
        enabled=data.enabled,
        is_custom=True,
    )
    db.add(macro)
    await db.commit()
    await db.refresh(macro)
    logger.info("Created custom macro: %s (event=%s)", macro.name, macro.event)
    return macro


@router.patch("/{macro_id}", response_model=MacroResponse)
async def update_macro(
    macro_id: int,
    data: MacroUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    """Update a macro (both custom and built-in can be edited)."""
    result = await db.execute(select(Macro).where(Macro.id == macro_id))
    macro = result.scalar_one_or_none()
    if not macro:
        raise HTTPException(404, "Macro not found")

    update_data = data.model_dump(exclude_unset=True)

    # Serialize printer_models to JSON
    if "printer_models" in update_data:
        update_data["printer_models"] = json.dumps(update_data["printer_models"])

    for field, value in update_data.items():
        setattr(macro, field, value)

    await db.commit()
    await db.refresh(macro)
    logger.info("Updated macro %s: %s", macro_id, list(update_data.keys()))
    return macro


@router.delete("/{macro_id}")
async def delete_macro(
    macro_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    """Delete a custom macro. Built-in macros cannot be deleted."""
    result = await db.execute(select(Macro).where(Macro.id == macro_id))
    macro = result.scalar_one_or_none()
    if not macro:
        raise HTTPException(404, "Macro not found")

    if not macro.is_custom:
        raise HTTPException(400, "Cannot delete built-in macros")

    await db.delete(macro)
    await db.commit()
    logger.info("Deleted custom macro %s (%s)", macro_id, macro.name)
    return {"message": "Macro deleted"}
