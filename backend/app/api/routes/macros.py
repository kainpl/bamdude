"""API routes for macro management."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.macro import Macro
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.schemas.macro import MacroCreate, MacroResponse, MacroUpdate
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/macros", tags=["macros"])

# Available macro events
MACRO_EVENTS = {
    "swap_mode_start": "Swap Mode - Start Sequence",
    "swap_mode_change_table": "Swap Mode - Change Table",
    "print_started": "Print Started (gcode_state → RUNNING)",
    "print_finished": "Print Finished (terminal status — completed/failed/cancelled)",
}


@router.get("/meta")
async def get_macro_meta():
    """Get metadata for macro UI: events, printer models, swap profiles, mqtt actions."""
    from backend.app.core.mqtt_macro_actions import catalog_for_meta
    from backend.app.core.swap_profiles import SWAP_PROFILES
    from backend.app.utils.printer_model_names import PRINTER_MODEL_DISPLAY_NAMES

    # Events that are meaningful only in swap-mode context - frontend uses
    # this list to decide when to show the "Swap profile" picker and to
    # hide swap_mode_only for non-swap events.
    swap_events = ["swap_mode_start", "swap_mode_change_table"]

    return {
        "events": MACRO_EVENTS,
        "swap_events": swap_events,
        "printer_models": PRINTER_MODEL_DISPLAY_NAMES,
        "swap_profiles": [{"id": pid, **profile} for pid, profile in SWAP_PROFILES.items()],
        "mqtt_actions": catalog_for_meta(),
    }


@router.get("/swap-profiles")
async def list_swap_profiles():
    """Return the canonical swap-profile catalog (public-ish read-only)."""
    from backend.app.core.swap_profiles import SWAP_PROFILES

    return [{"id": pid, **profile} for pid, profile in SWAP_PROFILES.items()]


@router.get("/", response_model=list[MacroResponse])
async def list_macros(
    printer_model: str | None = Query(None, description="Filter by printer model"),
    swap_mode: bool | None = Query(None, description="Filter by swap_mode_only"),
    event: str | None = Query(None, description="Filter by event"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
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
            m for m in macros if "*" in json.loads(m.printer_models) or printer_model in json.loads(m.printer_models)
        ]

    return macros


@router.get("/{macro_id}", response_model=MacroResponse)
async def get_macro(
    macro_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
):
    """Get a single macro."""
    result = await db.execute(select(Macro).where(Macro.id == macro_id))
    macro = result.scalar_one_or_none()
    if not macro:
        raise HTTPException(404, "Macro not found")
    return macro


def _validate_macro_action(action_type: str, mqtt_action: str | None, gcode: str | None) -> None:
    """Cross-field validation for action_type + mqtt_action + gcode.

    Reused by create and patch paths so errors stay consistent.
    Raises ``HTTPException`` on mismatch.
    """
    from backend.app.core.mqtt_macro_actions import MQTT_MACRO_ACTIONS

    if action_type not in ("gcode", "mqtt_action"):
        raise HTTPException(400, f"Unknown action_type '{action_type}'")

    if action_type == "mqtt_action":
        if not mqtt_action:
            raise HTTPException(400, "action_type='mqtt_action' requires mqtt_action to be set")
        if mqtt_action not in MQTT_MACRO_ACTIONS:
            raise HTTPException(
                400,
                f"Unknown mqtt_action '{mqtt_action}'. Valid options: {sorted(MQTT_MACRO_ACTIONS)}",
            )


@router.post("/", response_model=MacroResponse)
async def create_macro(
    data: MacroCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Create a custom macro."""
    _validate_macro_action(data.action_type, data.mqtt_action, data.gcode)

    macro = Macro(
        name=data.name,
        description=data.description,
        printer_models=json.dumps(data.printer_models),
        swap_mode_only=data.swap_mode_only,
        swap_profile=(data.swap_profile or None),
        event=data.event,
        action_type=data.action_type,
        mqtt_action=data.mqtt_action if data.action_type == "mqtt_action" else None,
        delay_seconds=data.delay_seconds,
        gcode=data.gcode,
        enabled=data.enabled,
        is_custom=True,
    )
    db.add(macro)
    await db.commit()
    await db.refresh(macro)
    logger.info("Created custom macro: %s (event=%s, type=%s)", macro.name, macro.event, macro.action_type)
    return macro


@router.patch("/{macro_id}", response_model=MacroResponse)
async def update_macro(
    macro_id: int,
    data: MacroUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
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

    # Treat empty-string swap_profile from the client as "clear binding".
    if update_data.get("swap_profile") == "":
        update_data["swap_profile"] = None

    # Validate action_type / mqtt_action pair against the catalog. Use the
    # incoming update when present, else fall back to the stored row.
    if "action_type" in update_data or "mqtt_action" in update_data:
        next_action_type = update_data.get("action_type", macro.action_type)
        next_mqtt_action = update_data.get("mqtt_action", macro.mqtt_action)
        next_gcode = update_data.get("gcode", macro.gcode)
        _validate_macro_action(next_action_type, next_mqtt_action, next_gcode)
        # Clear mqtt_action when switching back to gcode so we don't carry
        # stale bindings.
        if next_action_type == "gcode":
            update_data["mqtt_action"] = None

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
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
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


class MacroExecuteResponse(BaseModel):
    success: bool
    message: str
    sequence_id: int | None = None


@router.post("/{macro_id}/execute", response_model=MacroExecuteResponse)
async def execute_macro(
    macro_id: int,
    printer_id: int = Query(..., description="Printer to execute macro on"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_CONTROL),
):
    """Execute a macro on a specific printer by sending its GCode via MQTT."""
    # Get macro
    result = await db.execute(select(Macro).where(Macro.id == macro_id))
    macro = result.scalar_one_or_none()
    if not macro:
        raise HTTPException(404, "Macro not found")

    if not macro.enabled:
        raise HTTPException(400, "Macro is disabled")

    # Content guard depends on action_type: gcode needs non-empty gcode;
    # mqtt_action needs a valid catalog entry (validated at create/patch time
    # but we re-check here in case of stale rows from before migration m017).
    if macro.action_type == "gcode":
        if not macro.gcode or not macro.gcode.strip():
            raise HTTPException(400, "Macro has no GCode content")
    else:
        from backend.app.core.mqtt_macro_actions import get_action

        if not macro.mqtt_action or get_action(macro.mqtt_action) is None:
            raise HTTPException(400, f"Macro mqtt_action '{macro.mqtt_action}' is unknown")

    # Get printer
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    # Check model compatibility
    models = json.loads(macro.printer_models)
    if "*" not in models and printer.model not in models:
        raise HTTPException(400, f"Macro not compatible with printer model '{printer.model}'")

    # Check swap_mode requirement
    if macro.swap_mode_only and not printer.swap_mode_enabled:
        raise HTTPException(400, "Macro requires swap mode enabled on printer")

    # Check swap_profile binding - a profile-bound macro must not be executed
    # on a printer using a different profile, otherwise the wrong gcode fires.
    if macro.swap_profile and macro.swap_profile != printer.swap_profile:
        raise HTTPException(
            400,
            f"Macro targets swap profile '{macro.swap_profile}' but printer is set to "
            f"'{printer.swap_profile or 'none'}'",
        )

    # Ensure MQTT connection is fresh (reconnect if stale)
    if not await printer_manager.ensure_fresh_connection_for_printer(printer):
        raise HTTPException(400, "Printer MQTT connection failed")

    # Get MQTT client
    client = printer_manager.get_client(printer_id)
    if not client or not client.state or not client.state.connected:
        raise HTTPException(400, "Printer is not connected")

    from backend.app.services.macro_executor import (
        dispatch_mqtt_action,
        send_macro_and_await_ack,
    )

    logger.info(
        "[MACRO] Executing macro '%s' (type=%s) on '%s' (id=%d): event=%s, printer_state=%s",
        macro.name,
        macro.action_type,
        printer.name,
        printer.id,
        macro.event,
        client.state.state,
    )

    if macro.action_type == "mqtt_action":
        success, error_msg = dispatch_mqtt_action(client, macro.mqtt_action or "", macro.name)
    else:
        success, error_msg = await send_macro_and_await_ack(client, macro.gcode, macro.name, printer.model)

    if not success:
        if "acknowledge" in error_msg.lower():
            raise HTTPException(408, error_msg)
        raise HTTPException(400, error_msg)

    return MacroExecuteResponse(
        success=True,
        message=f"Macro '{macro.name}' executing",
    )
