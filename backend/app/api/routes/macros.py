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


@router.post("/", response_model=MacroResponse)
async def create_macro(
    data: MacroCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
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

    if not macro.gcode or not macro.gcode.strip():
        raise HTTPException(400, "Macro has no GCode content")

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

    # Ensure MQTT connection is fresh (reconnect if stale)
    if not await printer_manager.ensure_fresh_connection_for_printer(printer):
        raise HTTPException(400, "Printer MQTT connection failed")

    # Get MQTT client
    client = printer_manager.get_client(printer_id)
    if not client or not client.state or not client.state.connected:
        raise HTTPException(400, "Printer is not connected")

    # Wrap macro GCode with claim_action markers:
    # - Start: M1002 gcode_claim_action:11 sets stg_cur=11 (signals "macro executing")
    # - M400 S5 gives MQTT time to propagate the stage change
    # - End: M400 waits for moves, then claim_action:0 clears stg_cur back to 0
    import asyncio
    import threading

    raw_gcode = macro.gcode.strip()
    # stg_cur=0 ("Printing") signals macro executing; idle value depends on model
    # X1/X1C/X1E use stg_cur=-1 for idle, other models use 255
    model_upper = (printer.model or "").upper().replace("-", "").replace(" ", "")
    idle_stg = -1 if model_upper in ("X1", "X1C", "X1E") else 255
    gcode = f"M1002 gcode_claim_action : 0;\nM400 S5;\n{raw_gcode}\nM400;\nM1002 gcode_claim_action : {idle_stg};"

    # Set macro executing state before sending
    client.state.macro_executing = macro.name

    logger.info(
        "[MACRO] Sending macro '%s' to printer '%s' (id=%d): "
        "gcode_lines=%d, swap_mode=%s, event=%s, printer_state=%s, stg_cur=%s",
        macro.name,
        printer.name,
        printer.id,
        raw_gcode.count("\n") + 1,
        macro.swap_mode_only,
        macro.event,
        client.state.state,
        client.state.stg_cur,
    )

    # Send GCode and wait for ACK from printer
    sent = client.send_gcode(gcode)
    if not sent:
        client.state.macro_executing = None
        logger.warning("[MACRO] Failed to send macro '%s' — MQTT not connected", macro.name)
        return MacroExecuteResponse(success=False, message="Failed to send GCode to printer")

    seq_id = str(client._sequence_id)

    # Wait for printer ACK (result: success/failed) — typically <200ms
    ack_event = threading.Event()
    ack_result: dict = {"success": False, "reason": ""}
    client.register_ack_listener(seq_id, ack_event, ack_result)

    try:
        await asyncio.to_thread(ack_event.wait, 5.0)
    except Exception:
        pass

    if not ack_event.is_set():
        client._ack_listeners.pop(seq_id, None)
        client.state.macro_executing = None
        logger.warning("[MACRO] Timeout — no ACK for macro '%s' (seq=%s)", macro.name, seq_id)
        raise HTTPException(408, "Printer did not acknowledge command in time")

    if not ack_result["success"]:
        client.state.macro_executing = None
        logger.warning(
            "[MACRO] Printer rejected macro '%s' (seq=%s): %s",
            macro.name,
            seq_id,
            ack_result.get("reason", "unknown"),
        )
        raise HTTPException(400, f"Printer rejected macro: {ack_result.get('reason', 'unknown')}")

    logger.info(
        "[MACRO] ACK received — macro '%s' accepted by printer '%s' (seq=%s, result=%s, reason=%s)",
        macro.name,
        printer.name,
        seq_id,
        ack_result.get("success"),
        ack_result.get("reason", ""),
    )

    return MacroExecuteResponse(
        success=True,
        message=f"Macro '{macro.name}' executing",
        sequence_id=int(seq_id),
    )
