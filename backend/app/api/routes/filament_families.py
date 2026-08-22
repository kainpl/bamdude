"""Filament family catalog endpoints (spec A): family search over both
tiers, per-family presets for a printer, and the manual sync trigger."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset
from backend.app.schemas.filament_family import (
    AddPrintersRequest,
    ClonedRootOut,
    CreateFamilyRequest,
    CreateFamilyResponse,
)
from backend.app.services.filament_preset_sync import request_sync_soon
from backend.app.utils import filament_catalog as catalog

router = APIRouter(prefix="/filament-families", tags=["filament-families"])


@router.post("/sync")
async def trigger_preset_sync(_=RequirePermission(Permission.INVENTORY_READ)):
    """Poke the mirror loop. Debounced by the loop itself; returns immediately."""
    request_sync_soon()
    return {"queued": True}


@router.get("/authoring-options")
async def authoring_options(_=RequirePermission(Permission.INVENTORY_READ)):
    """Dialog data mirrored from BS (spec B §1) + per-ecosystem push
    capability (Orca ships designed-inactive — spec B §5)."""
    from backend.app.services.filament_authoring import FILAMENT_TYPES
    from backend.app.services.filament_push import PUSH_CAPABLE

    return {"filament_types": FILAMENT_TYPES, "push": PUSH_CAPABLE}


@router.post("", status_code=201, response_model=CreateFamilyResponse)
async def create_family_endpoint(
    req: CreateFamilyRequest,
    current_user: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Create a custom family (spec B §1–§2): identity + one root clone per
    printer; optional immediate push of the clones to Bambu Cloud."""
    from backend.app.services import filament_authoring as authoring

    try:
        result = await authoring.create_family(
            db,
            vendor=req.vendor,
            filament_type=req.filament_type,
            serial=req.serial,
            printer_ids=req.printer_ids,
            source_mode=req.source_mode,
            source=req.source,
            source_id=req.source_id,
            user=current_user,
        )
    except authoring.AuthoringError as e:
        raise HTTPException(400, str(e))
    push_results: list[dict] | None = None
    if req.push_to_bambu:
        from backend.app.services.filament_push import push_family

        try:
            push_results = await push_family(db, filament_id=result.filament_id, user=current_user)
        except authoring.AuthoringError as e:
            push_results = [{"status": "error", "detail": str(e)}]
    return CreateFamilyResponse(
        filament_id=result.filament_id,
        name=result.name,
        attached=result.attached,
        roots=[ClonedRootOut(**vars(r)) for r in result.roots],
        warnings=result.warnings,
        push=push_results,
    )


@router.post("/{filament_id}/printers", response_model=CreateFamilyResponse)
async def add_family_printers(
    filament_id: str,
    req: AddPrintersRequest,
    current_user: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """One more root clone with the same id (BS clone_presets_for_printer)."""
    from backend.app.services import filament_authoring as authoring

    try:
        result = await authoring.add_printers_to_family(
            db,
            filament_id=filament_id,
            printer_ids=req.printer_ids,
            source_mode=req.source_mode,
            source=req.source,
            source_id=req.source_id,
            user=current_user,
        )
    except authoring.AuthoringError as e:
        raise HTTPException(400, str(e))
    return CreateFamilyResponse(
        filament_id=result.filament_id,
        name=result.name,
        attached=result.attached,
        roots=[ClonedRootOut(**vars(r)) for r in result.roots],
        warnings=result.warnings,
    )


@router.post("/{filament_id}/push")
async def push_family_endpoint(
    filament_id: str,
    ecosystem: str = Query("bambu", pattern="^(bambu|orca)$"),
    current_user: User | None = RequirePermission(Permission.CLOUD_AUTH),
    db: AsyncSession = Depends(get_db),
):
    """Push (or explicitly re-push) the family's presets to the cloud."""
    from backend.app.services.filament_authoring import AuthoringError
    from backend.app.services.filament_push import push_family

    try:
        return {"results": await push_family(db, filament_id=filament_id, ecosystem=ecosystem, user=current_user)}
    except AuthoringError as e:
        raise HTTPException(400, str(e))


@router.delete("/{filament_id}")
async def delete_family_endpoint(
    filament_id: str,
    also_cloud: bool = Query(False),
    current_user: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Delete an authored family; refused (409) while spools / calibrations
    reference the id. ``also_cloud`` best-effort deletes pushed copies."""
    from backend.app.services import filament_authoring as authoring

    try:
        return await authoring.delete_family(db, filament_id=filament_id, also_cloud=also_cloud, user=current_user)
    except authoring.FamilyInUseError as e:
        raise HTTPException(409, {"detail": str(e), "spools": e.spools, "calibrations": e.calibrations})
    except authoring.AuthoringError as e:
        raise HTTPException(400, str(e))


async def _my_family_ids(db: AsyncSession) -> set[str]:
    """The user's own set — BamDude's equivalent of BS's AppConfig
    [filaments] "installed" section: families behind the user's cloud/local
    presets, linked to spools, or calibrated on a printer."""
    from backend.app.models.filament_calibration import FilamentCalibration
    from backend.app.models.spool import Spool

    ids: set[str] = set()
    for row in (await db.execute(select(UserFilamentPreset.family_filament_id).distinct())).scalars():
        if row:
            ids.add(row)
    for row in (await db.execute(select(Spool.filament_family_id).distinct())).scalars():
        if row:
            ids.add(row)
    for row in (await db.execute(select(FilamentCalibration.filament_id).distinct())).scalars():
        if row:
            ids.add(row)
    return ids


@router.get("")
async def list_families(
    q: str = Query("", max_length=100),
    limit: int = Query(50, ge=1, le=200),
    scope: str = Query("mine", pattern="^(mine|all)$"),
    _=RequirePermission(Permission.INVENTORY_READ),
    db: AsyncSession = Depends(get_db),
):
    """Families across both tiers. Default scope='mine' mirrors BS's
    "installed filaments" behaviour: the browse list is the families the
    user actually has (own presets' bases, spool links, calibrations,
    custom families); a non-empty search always sweeps the FULL catalog.
    Falls back to the full catalog when 'mine' is empty (fresh install)."""
    mine: set[str] | None = None
    if scope == "mine" and not q.strip():
        mine = await _my_family_ids(db)
        if not mine:
            mine = None  # fresh install — show everything
    out = [
        {
            "filament_id": f.filament_id,
            "ecosystem": "bambu",
            "alias": f.alias,
            "vendor": f.vendor,
            "filament_type": f.filament_type,
            "origin": "system",
        }
        for f in catalog.search_families(q, limit if mine is None else 1000)
        if mine is None or f.filament_id in mine
    ]
    needle = q.strip().lower()
    user_rows = (
        (await db.execute(select(UserFilamentFamily).where(UserFilamentFamily.orphaned.is_(False)))).scalars().all()
    )
    seen = {row["filament_id"] for row in out}
    for fam in user_rows:
        hay = f"{fam.alias} {fam.vendor or ''} {fam.filament_type or ''} {fam.filament_id}".lower()
        if fam.filament_id not in seen and (not needle or needle in hay):
            seen.add(fam.filament_id)
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
