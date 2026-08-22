"""Filament family catalog endpoints (spec A). The family list/preset
endpoints arrive with the consumer rework; this shell carries the manual
sync trigger."""

from fastapi import APIRouter

from backend.app.core.auth import RequirePermission
from backend.app.core.permissions import Permission
from backend.app.services.filament_preset_sync import request_sync_soon

router = APIRouter(prefix="/filament-families", tags=["filament-families"])


@router.post("/sync")
async def trigger_preset_sync(_=RequirePermission(Permission.INVENTORY_READ)):
    """Poke the mirror loop. Debounced by the loop itself; returns immediately."""
    request_sync_soon()
    return {"queued": True}
