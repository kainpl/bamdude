"""Filament family catalog endpoints (spec A): family search over both
tiers, per-family presets for a printer, and the manual sync trigger."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset
from backend.app.services.filament_preset_sync import request_sync_soon
from backend.app.utils import filament_catalog as catalog

router = APIRouter(prefix="/filament-families", tags=["filament-families"])


@router.post("/sync")
async def trigger_preset_sync(_=RequirePermission(Permission.INVENTORY_READ)):
    """Poke the mirror loop. Debounced by the loop itself; returns immediately."""
    request_sync_soon()
    return {"queued": True}


@router.get("")
async def list_families(
    q: str = Query("", max_length=100),
    limit: int = Query(50, ge=1, le=200),
    _=RequirePermission(Permission.INVENTORY_READ),
    db: AsyncSession = Depends(get_db),
):
    """Search families across both tiers: system catalog + user families
    (custom P-hashes from the cloud mirrors / authoring)."""
    out = [
        {
            "filament_id": f.filament_id,
            "ecosystem": "bambu",
            "alias": f.alias,
            "vendor": f.vendor,
            "filament_type": f.filament_type,
            "origin": "system",
        }
        for f in catalog.search_families(q, limit)
    ]
    needle = q.strip().lower()
    user_rows = (
        (await db.execute(select(UserFilamentFamily).where(UserFilamentFamily.orphaned.is_(False)))).scalars().all()
    )
    seen = {row["filament_id"] for row in out}
    for fam in user_rows:
        hay = f"{fam.alias} {fam.vendor or ''} {fam.filament_type or ''}".lower()
        if fam.filament_id not in seen and (not needle or needle in hay):
            out.append(
                {
                    "filament_id": fam.filament_id,
                    "ecosystem": fam.ecosystem,
                    "alias": fam.alias,
                    "vendor": fam.vendor,
                    "filament_type": fam.filament_type,
                    "origin": fam.origin,
                }
            )
    return sorted(out, key=lambda r: r["alias"])[:limit]


@router.get("/{filament_id}/presets")
async def family_presets(
    filament_id: str,
    printer_name: str = Query(""),
    _=RequirePermission(Permission.INVENTORY_READ),
    db: AsyncSession = Depends(get_db),
):
    """The family's presets: system ones (filtered to the printer when
    given — BS preset names are '<display name> <d> nozzle') plus the user's
    mirrored cloud/local presets of that family."""
    rows = []
    for preset in catalog.presets_for_family(filament_id):
        if printer_name and printer_name not in preset.compatible_printers:
            continue
        rows.append(
            {
                "name": preset.name,
                "setting_id": preset.setting_id,
                "nozzle_temp_min": preset.nozzle_temp_min,
                "nozzle_temp_max": preset.nozzle_temp_max,
                "compatible_printers": list(preset.compatible_printers),
            }
        )
    mirror = (
        (await db.execute(select(UserFilamentPreset).where(UserFilamentPreset.family_filament_id == filament_id)))
        .scalars()
        .all()
    )
    for row in mirror:
        rows.append(
            {
                "name": row.name,
                "setting_id": row.cloud_id or "",
                "nozzle_temp_min": row.nozzle_temp_min,
                "nozzle_temp_max": row.nozzle_temp_max,
                "compatible_printers": [],
            }
        )
    return rows
