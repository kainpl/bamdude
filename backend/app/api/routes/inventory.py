import csv
import io
import json
import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.auth import RequireAnyPermission, RequirePermission
from backend.app.core.catalog_defaults import DEFAULT_COLOR_CATALOG, DEFAULT_SPOOL_CATALOG
from backend.app.core.config import APP_VERSION
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.websocket import ws_manager
from backend.app.models.ams_label import AmsLabel
from backend.app.models.color_catalog import ColorCatalogEntry
from backend.app.models.location import Location
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_catalog import SpoolCatalogEntry
from backend.app.models.spool_k_profile import SpoolKProfile
from backend.app.models.user import User
from backend.app.schemas.archive import PaginationMeta
from backend.app.schemas.forecast import (
    ForecastChartResponse,
    ForecastChartSeries,
    ForecastChartSku,
    ForecastListPage,
    ForecastLogisticsRow,
    SkuForecastRowResponse,
)
from backend.app.schemas.location import LocationCreate, LocationResponse, LocationUpdate
from backend.app.schemas.spool import (
    SpoolAssignmentCreate,
    SpoolAssignmentResponse,
    SpoolBulkCreate,
    SpoolBulkIds,
    SpoolBulkUpdate,
    SpoolCreate,
    SpoolKProfileBase,
    SpoolKProfileResponse,
    SpoolListItem,
    SpoolListPage,
    SpoolResponse,
    SpoolUpdate,
)
from backend.app.schemas.spool_usage import SpoolUsageHistoryResponse
from backend.app.services import forecast_engine, inventory_service
from backend.app.services.location_service import (
    DUPLICATE_LOCATION_NAME,
    assign_location_name,
    count_internal_spools_at_location,
    get_location_by_id,
    get_location_by_name,
    location_name_key,
    prepare_internal_spool_payload,
    rename_location as rename_location_record,
)
from backend.app.services.spool_csv import (
    MAX_CSV_IMPORT_BYTES,
    ImportPreview,
    ImportResult,
    parse_and_validate,
    serialize,
)
from backend.app.services.spoolman import SpoolmanClient, get_spoolman_client, init_spoolman_client
from backend.app.utils.filament_remaining import grams_used
from backend.app.utils.tag_normalization import normalize_tag_uid, normalize_tray_uuid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inventory", tags=["inventory"])


async def _validate_family_id(db: AsyncSession, family_id: str | None) -> None:
    """422 on a family id the catalog+mirrors cannot resolve — the picker
    only offers known ones, so garbage means a broken client."""
    if not family_id:
        return
    from backend.app.services.filament_identity import resolve_tray

    if (await resolve_tray(db, family_id)).family is None:
        raise HTTPException(status_code=422, detail="unknown filament family")


async def _safe_autolink(db: AsyncSession, spool: Spool) -> None:
    """Auto-link K-profiles for a spool, fail-silent so a link error never
    fails the spool write (mirrors printer_manager's sync wrapper)."""
    from backend.app.services.kprofile_autolink import autolink_spool

    try:
        await autolink_spool(db=db, spool=spool)
        await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("Auto-link of K-profiles for spool %s failed: %s", getattr(spool, "id", "?"), e)


# FilamentColors.xyz API
FILAMENT_COLORS_API = "https://filamentcolors.xyz/api"


async def apply_spool_to_slot_via_mqtt(
    *,
    db: AsyncSession,
    current_user: User | None,
    spool: Spool,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    current_tray_info_idx: str = "",
    current_tray_type: str = "",
) -> bool:
    """Publish ams_filament_setting + extrusion_cali_sel for a spool on a slot.

    Shared by `assign_spool` (initial assign for a loaded slot) and
    `on_ams_change` (replay when an empty pre-assigned slot transitions to
    loaded). Returns True when MQTT commands were published, False if no
    client was available.

    `current_tray_info_idx` / `current_tray_type` are live-tray hints used as
    fallback when the spool's slicer_filament can't be resolved. Adapted from
    upstream Bambuddy `b42aaca5` (#1247).
    """
    from backend.app.services.printer_manager import printer_manager

    client = printer_manager.get_client(printer_id)
    if client is None:
        return False

    state = printer_manager.get_status(printer_id)

    tray_type = spool.material
    tray_sub_brands = (
        f"{spool.brand} {spool.material} {spool.subtype}".strip()
        if spool.brand
        else f"{spool.material} {spool.subtype}"
        if spool.subtype
        else spool.material
    )
    tray_color = spool.rgba or "FFFFFFFF"

    nozzle_diameter = "0.4"
    if state and state.nozzles:
        nd = state.nozzles[0].nozzle_diameter
        if nd:
            nozzle_diameter = nd
    try:
        nozzle_dia_float = float(nozzle_diameter)
    except (TypeError, ValueError):
        nozzle_dia_float = 0.4

    # Determine slot's extruder from ams_extruder_map (feeds the K half below)
    slot_extruder = None
    if state and state.ams_extruder_map:
        if ams_id == 255:
            # External slots: ext-L (tray 0) → extruder 1, ext-R (tray 1) → extruder 0
            slot_extruder = 1 - tray_id
        else:
            slot_extruder = state.ams_extruder_map.get(str(ams_id))

    # ONE identity path (spec A §5.2): the family catalog builds the payload —
    # tray_info_idx = the spool's family, versioned setting_id from the
    # catalog, temps from the preset for this printer (spool overrides win
    # inside the builder). current_tray_info_idx / current_tray_type are
    # accepted for signature stability but no longer consulted — the family
    # model does not reuse a foreign tray id.
    from backend.app.services.slot_assignment import build_slot_assignment

    # ⚠️ The model lives in the manager's model cache, NOT on PrinterInfo
    # (name + serial only) — ``info.model`` was an AttributeError on every
    # call; only mocks (auto-attributes) kept it green.
    plan = await build_slot_assignment(
        db,
        spool=spool,
        printer_model=printer_manager.get_model(printer_id),
        nozzle_diameter=nozzle_diameter,
        supports_user_preset=bool(getattr(state, "support_user_preset", False)),
    )
    for note in plan.warnings:
        logger.info("Spool assign: %s", note)
    effective_tray_info_idx = plan.tray_info_idx
    effective_setting_id = plan.setting_id
    tray_type = plan.tray_type or tray_type
    temp_min, temp_max = plan.nozzle_temp_min, plan.nozzle_temp_max

    # a. Set filament setting
    client.ams_set_filament_setting(
        ams_id=ams_id,
        tray_id=tray_id,
        tray_info_idx=effective_tray_info_idx,
        tray_type=tray_type,
        tray_sub_brands=tray_sub_brands,
        tray_color=tray_color,
        nozzle_temp_min=temp_min,
        nozzle_temp_max=temp_max,
        setting_id=effective_setting_id,
        cols=plan.cols,
        ctype=plan.ctype,
    )

    # Register a read-back verification so the next AMS pushes can confirm the
    # tray actually accepted this assignment (upstream #2582). We record the same
    # effective filament id we pushed; the client fires on_assignment_verified on
    # match/timeout. Colour is informational only — the match keys on the filament
    # id the printer echoes back. ``cali_idx`` starts unknown because our K-profile
    # push below resolves it live inside ``apply_active_calibration_to_slot``,
    # which calls ``note_assignment_cali_idx`` to fill it in.
    client.register_assignment_verification(
        ams_id=ams_id,
        tray_id=tray_id,
        tray_info_idx=effective_tray_info_idx,
        tray_color=tray_color,
        cali_idx=None,
    )

    # b. Push extrusion calibration via the unified helper. The helper
    # re-resolves cali_idx live (stable-identity match) and fires
    # extrusion_cali_sel; stored cali_idx is just a hint.
    from backend.app.services.calibration_service import (
        apply_active_calibration_to_slot,
        derive_effective_filament_id,
    )

    effective_filament_id = await derive_effective_filament_id(
        spool=spool, slot_tray_info_idx=effective_tray_info_idx or None, db=db
    )
    nozzle_vt = str(getattr(state, "nozzle_volume_type", "standard") or "standard")
    if effective_filament_id:
        await apply_active_calibration_to_slot(
            db=db,
            printer_id=printer_id,
            ams_id=ams_id,
            slot_id=tray_id,
            filament_id=effective_filament_id,
            nozzle_diameter=nozzle_dia_float,
            nozzle_volume_type=nozzle_vt,
            extruder_id=slot_extruder if slot_extruder is not None else 0,
            spool_id=spool.id,
        )

    # (slot_preset_mappings retired — slot display names come from the
    # identity resolver now; see /cloud/filament-info.)

    logger.info(
        "Auto-configured AMS slot ams=%d tray=%d for spool %d on printer %d",
        ams_id,
        tray_id,
        spool.id,
        printer_id,
    )
    return True


# ── Spool Catalog Schemas ──────────────────────────────────────────────────


class CatalogEntryResponse(BaseModel):
    id: int
    name: str
    weight: int
    is_default: bool

    class Config:
        from_attributes = True


class CatalogEntryCreate(BaseModel):
    name: str
    weight: int


class CatalogEntryUpdate(BaseModel):
    name: str
    weight: int


class BulkDeleteIdsRequest(BaseModel):
    ids: list[int]


# ── Color Catalog Schemas ──────────────────────────────────────────────────


class ColorEntryResponse(BaseModel):
    id: int
    manufacturer: str
    color_name: str
    hex_color: str
    material: str | None
    is_default: bool
    # Optional gradient stops + visual effect — same shape the spool form
    # already carries on ``Spool``. Set on a catalog entry so picking it in
    # the Spool Form's catalog picker applies the full preset look, not
    # just hex + name (upstream Bambuddy #1340 / m076).
    extra_colors: str | None = None
    effect_type: str | None = None

    class Config:
        from_attributes = True


class ColorEntryCreate(BaseModel):
    manufacturer: str
    color_name: str
    hex_color: str
    material: str | None = None
    extra_colors: str | None = Field(default=None, max_length=255)
    effect_type: str | None = Field(default=None, max_length=20)


class ColorEntryUpdate(BaseModel):
    manufacturer: str
    color_name: str
    hex_color: str
    material: str | None = None
    extra_colors: str | None = Field(default=None, max_length=255)
    effect_type: str | None = Field(default=None, max_length=20)


class ColorLookupResult(BaseModel):
    found: bool
    hex_color: str | None = None
    material: str | None = None


class ColorByMaterialResult(BaseModel):
    color_name: str | None = None


# ── Spool Catalog CRUD ─────────────────────────────────────────────────────


@router.get("/catalog", response_model=list[CatalogEntryResponse])
async def get_spool_catalog(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """Get all spool catalog entries."""
    result = await db.execute(select(SpoolCatalogEntry).order_by(SpoolCatalogEntry.name))
    return list(result.scalars().all())


@router.post("/catalog", response_model=CatalogEntryResponse)
async def add_catalog_entry(
    entry: CatalogEntryCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Add a new spool catalog entry."""
    row = SpoolCatalogEntry(name=entry.name, weight=entry.weight, is_default=False)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.put("/catalog/{entry_id}", response_model=CatalogEntryResponse)
async def update_catalog_entry(
    entry_id: int,
    entry: CatalogEntryUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Update a spool catalog entry."""
    result = await db.execute(select(SpoolCatalogEntry).where(SpoolCatalogEntry.id == entry_id))
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Entry not found")
    row.name = entry.name
    row.weight = entry.weight
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/catalog/{entry_id}")
async def delete_catalog_entry(
    entry_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Delete a spool catalog entry."""
    result = await db.execute(select(SpoolCatalogEntry).where(SpoolCatalogEntry.id == entry_id))
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Entry not found")
    await db.delete(row)
    await db.commit()
    return {"status": "deleted"}


@router.post("/catalog/bulk-delete")
async def bulk_delete_catalog_entries(
    data: BulkDeleteIdsRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Delete multiple spool catalog entries by ID."""
    if not data.ids:
        return {"deleted": 0}
    result = await db.execute(select(SpoolCatalogEntry).where(SpoolCatalogEntry.id.in_(data.ids)))
    rows = result.scalars().all()
    for row in rows:
        await db.delete(row)
    await db.commit()
    return {"deleted": len(rows)}


@router.post("/catalog/reset")
async def reset_spool_catalog(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Reset spool catalog to defaults."""
    await db.execute(select(SpoolCatalogEntry))  # ensure table loaded
    # Delete all
    result = await db.execute(select(SpoolCatalogEntry))
    for row in result.scalars().all():
        await db.delete(row)
    # Re-seed defaults
    for name, weight in DEFAULT_SPOOL_CATALOG:
        db.add(SpoolCatalogEntry(name=name, weight=weight, is_default=True))
    await db.commit()
    return {"status": "reset"}


# ── Storage Locations (upstream #1505) ──────────────────────────────────────


async def _load_settings_map(db: AsyncSession) -> dict[str, str]:
    result = await db.execute(select(Settings))
    return {s.key: s.value for s in result.scalars().all()}


def _spoolman_is_enabled(settings: dict[str, str]) -> bool:
    return settings.get("spoolman_enabled", "false").lower() == "true"


async def _ensure_spoolman_client(settings: dict[str, str]) -> SpoolmanClient | None:
    if not _spoolman_is_enabled(settings):
        return None
    url = settings.get("spoolman_url", "").strip()
    if not url:
        return None
    from backend.app.api.routes._spoolman_helpers import assert_safe_spoolman_url

    try:
        assert_safe_spoolman_url(url)
    except ValueError:
        return None
    client = await get_spoolman_client()
    # BamDude's SpoolmanClient exposes ``api_url`` (== f"{base_url}/api/v1");
    # compare on it per the fork's client contract, deriving the /api/v1 suffix
    # so a cached client for the same URL is reused rather than re-created.
    if not client or client.api_url != f"{url.rstrip('/')}/api/v1":
        client = await init_spoolman_client(url)
    return client


async def _spool_counts_for_locations(
    db: AsyncSession,
    locations: list[Location],
    settings: dict[str, str],
) -> dict[int, int]:
    if _spoolman_is_enabled(settings):
        client = await _ensure_spoolman_client(settings)
        if client:
            try:
                spools = await client.get_all_spools(allow_archived=False)
            except Exception:
                logger.warning("Failed to fetch Spoolman spools for location counts", exc_info=True)
            else:
                # Use the canonical key helper so this matches what the
                # migration backfill, Location.name_key, and every other
                # codepath store as the case-insensitive lookup key. Plain
                # str.lower() drifts for non-ASCII (Turkish ı/İ, German ß)
                # and caused mismatched delete-block counts in Spoolman mode.
                by_key: dict[str, int] = {}
                for spool in spools:
                    raw = spool.get("location")
                    if not raw or not isinstance(raw, str) or not raw.strip():
                        continue
                    try:
                        key = location_name_key(raw)
                    except ValueError:
                        continue
                    by_key[key] = by_key.get(key, 0) + 1
                return {loc.id: by_key.get(loc.name_key, 0) for loc in locations}

    counts: dict[int, int] = {}
    for loc in locations:
        counts[loc.id] = await count_internal_spools_at_location(db, loc.id)
    return counts


def _location_to_response(location: Location, spool_count: int) -> LocationResponse:
    return LocationResponse(
        id=location.id,
        name=location.name,
        identifier=location.identifier,
        spool_count=spool_count,
        created_at=location.created_at,
        updated_at=location.updated_at,
    )


@router.get("/locations", response_model=list[LocationResponse])
async def list_locations(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """List all storage locations with spool counts."""
    settings = await _load_settings_map(db)
    result = await db.execute(select(Location).order_by(Location.name))
    locations = list(result.scalars().all())
    counts = await _spool_counts_for_locations(db, locations, settings)
    return [_location_to_response(loc, counts.get(loc.id, 0)) for loc in locations]


@router.post("/locations", response_model=LocationResponse, status_code=201)
async def create_location(
    data: LocationCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Create a storage location."""
    existing = await get_location_by_name(db, data.name)
    if existing:
        raise HTTPException(status_code=409, detail=DUPLICATE_LOCATION_NAME)
    location = Location(identifier=data.identifier)
    assign_location_name(location, data.name)
    db.add(location)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=DUPLICATE_LOCATION_NAME) from exc
    await db.refresh(location)
    await ws_manager.broadcast({"type": "inventory_changed"})
    return _location_to_response(location, 0)


@router.patch("/locations/{location_id}", response_model=LocationResponse)
async def update_location(
    location_id: int,
    data: LocationUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Update a storage location (rename propagates to assigned spools)."""
    location = await get_location_by_id(db, location_id)
    if not location:
        raise HTTPException(status_code=404, detail="Location not found")

    old_name = location.name
    if data.identifier is not None:
        location.identifier = data.identifier or None

    if data.name is not None and data.name != old_name:
        try:
            await rename_location_record(db, location, data.name)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # Cascade to Spoolman BEFORE the local commit so a Spoolman failure
        # rolls back the local rename instead of leaving the catalog and
        # Spoolman's per-spool `location` field permanently diverged. Without
        # this ordering, a partial failure makes the next location-sync recreate
        # the old name as a duplicate catalog row (#1505 review blocker).
        settings = await _load_settings_map(db)
        client = await _ensure_spoolman_client(settings)
        if client:
            try:
                await client.rename_location(old_name, location.name)
            except Exception as exc:
                logger.warning(
                    "Spoolman location rename failed for %s -> %s: %s",
                    old_name,
                    location.name,
                    exc,
                )
                await db.rollback()
                raise HTTPException(
                    status_code=502,
                    detail="Spoolman rename failed; local rename rolled back",
                ) from exc

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=DUPLICATE_LOCATION_NAME) from exc
    await db.refresh(location)
    settings = await _load_settings_map(db)
    counts = await _spool_counts_for_locations(db, [location], settings)
    await ws_manager.broadcast({"type": "inventory_changed"})
    return _location_to_response(location, counts.get(location.id, 0))


@router.delete("/locations/{location_id}")
async def delete_location(
    location_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Delete a storage location when no spools are assigned."""
    location = await get_location_by_id(db, location_id)
    if not location:
        raise HTTPException(status_code=404, detail="Location not found")

    settings = await _load_settings_map(db)
    counts = await _spool_counts_for_locations(db, [location], settings)
    if counts.get(location.id, 0) > 0:
        raise HTTPException(status_code=409, detail="Location has spools assigned and cannot be deleted")

    await db.delete(location)
    await db.commit()
    await ws_manager.broadcast({"type": "inventory_changed"})
    return {"status": "deleted"}


# ── Color Catalog CRUD ─────────────────────────────────────────────────────


@router.get("/colors", response_model=list[ColorEntryResponse])
async def get_color_catalog(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """Get all color catalog entries."""
    result = await db.execute(
        select(ColorCatalogEntry).order_by(
            ColorCatalogEntry.manufacturer, ColorCatalogEntry.material, ColorCatalogEntry.color_name
        )
    )
    return list(result.scalars().all())


@router.get("/colors/map")
async def get_color_name_map(
    db: AsyncSession = Depends(get_db),
):
    """Compact {hex: name} map for frontend color-name resolution.

    Not gated on INVENTORY_READ - every page that renders a spool color needs
    this, including read-only views available to users without inventory access.
    Normalized to lowercase 6-char hex without '#'. When multiple catalog entries
    share the same hex (different materials or manufacturers), Bambu Lab wins,
    then default entries, then the first encountered.
    """
    result = await db.execute(
        select(
            ColorCatalogEntry.hex_color,
            ColorCatalogEntry.color_name,
            ColorCatalogEntry.manufacturer,
            ColorCatalogEntry.is_default,
        )
    )
    mapping: dict[str, tuple[str, int]] = {}  # hex → (name, priority); higher priority wins
    for hex_color, color_name, manufacturer, is_default in result.all():
        if not hex_color or not color_name:
            continue
        key = hex_color.lstrip("#").lower()[:6]
        if len(key) != 6:
            continue
        priority = 0
        if manufacturer and manufacturer.strip().lower() == "bambu lab":
            priority += 2
        if is_default:
            priority += 1
        existing = mapping.get(key)
        if existing is None or priority > existing[1]:
            mapping[key] = (color_name, priority)
    return {"colors": {k: v[0] for k, v in mapping.items()}}


@router.get("/colors/by-material", response_model=ColorByMaterialResult)
async def get_color_by_material(
    hex: str,
    material: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Disambiguated hex→name lookup that respects material context.

    ``/colors/map`` collapses every catalog entry sharing a hex to a single
    name with "Bambu Lab > is_default > first" priority — that loses, e.g.,
    "PLA Matte Charcoal" (#000000) behind "PLA Basic Black" (also #000000).
    This endpoint preserves the material context so the queue scheduler's
    Filament Override / Mapping label can show the actually-sliced sub-brand
    colour instead of the generic bucket. Upstream Bambuddy #1718.

    Returns ``color_name=None`` when the hex isn't in the catalog at all.
    When the hex IS in the catalog but no entry matches the requested
    material (or none was supplied), falls back to the same priority order
    as ``/colors/map`` so callers without a material hint don't regress.

    Not gated on INVENTORY_READ for the same reason ``/colors/map`` isn't —
    every queue / archive view that renders a sliced filament colour needs
    this, including read-only roles.
    """
    key = hex.lstrip("#").lower()[:6]
    if len(key) != 6:
        return ColorByMaterialResult(color_name=None)

    material_norm = (material or "").strip().lower()

    # Catalog rows are stored as ``#RRGGBB`` for the defaults, but legacy /
    # imported rows can lack the leading ``#`` (mirrors the ``/colors/map``
    # normalization). Strip ``#`` and lower-case on both sides so mixed-case
    # and hash-optional writes still match.
    result = await db.execute(
        select(
            ColorCatalogEntry.color_name,
            ColorCatalogEntry.manufacturer,
            ColorCatalogEntry.material,
            ColorCatalogEntry.is_default,
        ).where(func.lower(func.replace(ColorCatalogEntry.hex_color, "#", "")) == key)
    )
    candidates = [(name, mfg, mat, is_default) for name, mfg, mat, is_default in result.all() if name]
    if not candidates:
        return ColorByMaterialResult(color_name=None)

    if material_norm:
        for name, _mfg, mat, _is_default in candidates:
            if mat and mat.strip().lower() == material_norm:
                return ColorByMaterialResult(color_name=name)

    # Same priority order as ``/colors/map`` so a caller passing no (or an
    # unrecognised) material gets the existing answer, not a degraded one.
    best_name: str | None = None
    best_priority = -1
    for name, mfg, _mat, is_default in candidates:
        priority = 0
        if mfg and mfg.strip().lower() == "bambu lab":
            priority += 2
        if is_default:
            priority += 1
        if priority > best_priority:
            best_name = name
            best_priority = priority
    return ColorByMaterialResult(color_name=best_name)


@router.post("/colors", response_model=ColorEntryResponse)
async def add_color_entry(
    entry: ColorEntryCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Add a new color catalog entry."""
    row = ColorCatalogEntry(
        manufacturer=entry.manufacturer,
        color_name=entry.color_name,
        hex_color=entry.hex_color,
        material=entry.material,
        extra_colors=entry.extra_colors,
        effect_type=entry.effect_type,
        is_default=False,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.put("/colors/{entry_id}", response_model=ColorEntryResponse)
async def update_color_entry(
    entry_id: int,
    entry: ColorEntryUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Update a color catalog entry."""
    result = await db.execute(select(ColorCatalogEntry).where(ColorCatalogEntry.id == entry_id))
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Entry not found")
    row.manufacturer = entry.manufacturer
    row.color_name = entry.color_name
    row.hex_color = entry.hex_color
    row.material = entry.material
    row.extra_colors = entry.extra_colors
    row.effect_type = entry.effect_type
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/colors/{entry_id}")
async def delete_color_entry(
    entry_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Delete a color catalog entry."""
    result = await db.execute(select(ColorCatalogEntry).where(ColorCatalogEntry.id == entry_id))
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Entry not found")
    await db.delete(row)
    await db.commit()
    return {"status": "deleted"}


@router.post("/colors/bulk-delete")
async def bulk_delete_color_entries(
    data: BulkDeleteIdsRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Delete multiple color catalog entries by ID."""
    if not data.ids:
        return {"deleted": 0}
    result = await db.execute(select(ColorCatalogEntry).where(ColorCatalogEntry.id.in_(data.ids)))
    rows = result.scalars().all()
    for row in rows:
        await db.delete(row)
    await db.commit()
    return {"deleted": len(rows)}


@router.post("/colors/reset")
async def reset_color_catalog(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Reset color catalog to defaults."""
    result = await db.execute(select(ColorCatalogEntry))
    for row in result.scalars().all():
        await db.delete(row)
    for manufacturer, color_name, hex_color, material in DEFAULT_COLOR_CATALOG:
        db.add(
            ColorCatalogEntry(
                manufacturer=manufacturer,
                color_name=color_name,
                hex_color=hex_color,
                material=material,
                is_default=True,
            )
        )
    await db.commit()
    return {"status": "reset"}


@router.get("/colors/lookup", response_model=ColorLookupResult)
async def lookup_color(
    manufacturer: str,
    color_name: str,
    material: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """Look up a color by manufacturer and color name."""
    query = select(ColorCatalogEntry).where(
        ColorCatalogEntry.manufacturer == manufacturer,
        ColorCatalogEntry.color_name == color_name,
    )
    if material:
        query = query.where(ColorCatalogEntry.material == material)
    query = query.limit(1)
    result = await db.execute(query)
    row = result.scalar_one_or_none()
    if row:
        return ColorLookupResult(found=True, hex_color=row.hex_color, material=row.material)
    return ColorLookupResult(found=False)


@router.get("/colors/search", response_model=list[ColorEntryResponse])
async def search_colors(
    manufacturer: str | None = None,
    material: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """Search colors by manufacturer and/or material."""
    query = select(ColorCatalogEntry)
    if manufacturer:
        query = query.where(func.lower(ColorCatalogEntry.manufacturer).contains(manufacturer.lower()))
    if material:
        query = query.where(func.lower(ColorCatalogEntry.material).contains(material.lower()))
    query = query.order_by(ColorCatalogEntry.manufacturer, ColorCatalogEntry.color_name).limit(100)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.post("/colors/sync")
async def sync_from_filamentcolors(
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Sync colors from FilamentColors.xyz API with progress streaming."""

    async def generate():
        from backend.app.core.database import async_session

        added = 0
        skipped = 0
        total_fetched = 0
        total_available = 0

        try:
            # Identify honestly as BamDude rather than leaking httpx's default
            # "python-httpx/x.y" UA — consistent with every other outbound
            # client (bambu_cloud, makerworld, firmware check).
            async with httpx.AsyncClient(
                timeout=120.0,
                headers={"User-Agent": f"BamDude/{APP_VERSION} (+https://github.com/kainpl/bamdude)"},
            ) as client:
                page = 1
                while True:
                    response = await client.get(
                        f"{FILAMENT_COLORS_API}/swatch/",
                        params={"page": page},
                    )
                    response.raise_for_status()
                    data = response.json()
                    total_available = data.get("count", total_available)
                    results = data.get("results", [])
                    if not results:
                        break

                    async with async_session() as db:
                        for swatch in results:
                            total_fetched += 1
                            manufacturer_data = swatch.get("manufacturer")
                            manufacturer_name = (
                                manufacturer_data.get("name", "") if isinstance(manufacturer_data, dict) else ""
                            )
                            filament_type_data = swatch.get("filament_type")
                            mat = filament_type_data.get("name", "") if isinstance(filament_type_data, dict) else None
                            color_name_val = swatch.get("color_name", "")
                            hex_color_val = swatch.get("hex_color", "")

                            if not manufacturer_name or not color_name_val or not hex_color_val:
                                skipped += 1
                                continue

                            if not hex_color_val.startswith("#"):
                                hex_color_val = f"#{hex_color_val}"

                            # Check if entry already exists
                            existing = await db.execute(
                                select(ColorCatalogEntry)
                                .where(
                                    ColorCatalogEntry.manufacturer == manufacturer_name,
                                    ColorCatalogEntry.color_name == color_name_val,
                                    ColorCatalogEntry.material == mat,
                                )
                                .limit(1)
                            )
                            if existing.scalar_one_or_none():
                                skipped += 1
                            else:
                                db.add(
                                    ColorCatalogEntry(
                                        manufacturer=manufacturer_name,
                                        color_name=color_name_val,
                                        hex_color=hex_color_val.upper(),
                                        material=mat,
                                        is_default=False,
                                    )
                                )
                                added += 1

                        await db.commit()

                    progress = {
                        "type": "progress",
                        "added": added,
                        "skipped": skipped,
                        "total_fetched": total_fetched,
                        "total_available": total_available,
                    }
                    yield f"data: {json.dumps(progress)}\n\n"

                    if not data.get("next") or total_fetched >= total_available:
                        break
                    page += 1

            result = {
                "type": "complete",
                "added": added,
                "skipped": skipped,
                "total_fetched": total_fetched,
                "total_available": total_available,
            }
            yield f"data: {json.dumps(result)}\n\n"

        except httpx.HTTPError as e:
            logger.error("HTTP error syncing from FilamentColors.xyz: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        except Exception as e:
            logger.error("Error syncing from FilamentColors.xyz: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'error': 'Unexpected error during sync'})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── Spool CRUD ───────────────────────────────────────────────────────────────

# Every endpoint taking ``location_id`` MUST declare it with this pattern —
# anything else reaches ``int(location_id)`` in ``build_spool_filters``, where
# a non-numeric value raises ValueError uncaught into a 500 (T1 review finding
# 3). One shared constant so the list/ids endpoints can't drift apart; a
# pattern mismatch becomes a clean 422 like any other bad param.
_LOCATION_ID_PATTERN = r"^(__none__|\d+)$"

# Sanity cap for ``GET /spools/ids`` (spec §3.4): nobody bulk-edits this many
# spools; a bigger answer means a pathological call, refused with a 400 rather
# than materializing an unbounded id list.
_SPOOL_IDS_CAP = 50_000


def _spool_to_list_item(s: Spool, *, include_k_profiles: bool = False) -> SpoolListItem:
    """Slim list-row projection — every ``SpoolListItem`` field, built
    explicitly (never ``model_validate(s)``: ``k_profile_count`` has no
    matching ORM attribute). ``s.k_profiles`` must already be eager-loaded
    (``list_spools`` always ``selectinload``s it) — the async ORM has no
    implicit lazy load, so touching an unloaded relationship here would raise,
    not silently N+1.

    ``include_k_profiles`` (task 4, 2026-08-29): serialize the full
    ``k_profiles`` array too — the cards-view opt-in (see
    ``SpoolListItem``'s docstring). The rows are eager-loaded regardless, so
    this is a serialization-only switch, never an extra query."""
    return SpoolListItem(
        id=s.id,
        material=s.material,
        subtype=s.subtype,
        color_name=s.color_name,
        rgba=s.rgba,
        brand=s.brand,
        label_weight=s.label_weight,
        core_weight=s.core_weight,
        core_weight_catalog_id=s.core_weight_catalog_id,
        weight_used=s.weight_used,
        weight_used_baseline=s.weight_used_baseline,
        slicer_filament=s.slicer_filament,
        slicer_filament_name=s.slicer_filament_name,
        filament_family_id=s.filament_family_id,
        nozzle_temp_min=s.nozzle_temp_min,
        nozzle_temp_max=s.nozzle_temp_max,
        note=s.note,
        added_full=s.added_full,
        tag_uid=s.tag_uid,
        tray_uuid=s.tray_uuid,
        data_origin=s.data_origin,
        tag_type=s.tag_type,
        cost_per_kg=s.cost_per_kg,
        purchase_date=s.purchase_date,
        last_used=s.last_used,
        encode_time=s.encode_time,
        filament_diameter=s.filament_diameter,
        lot=s.lot,
        weight_locked=s.weight_locked,
        last_scale_weight=s.last_scale_weight,
        last_weighed_at=s.last_weighed_at,
        extra_colors=s.extra_colors,
        effect_type=s.effect_type,
        category=s.category,
        low_stock_threshold_pct=s.low_stock_threshold_pct,
        storage_location=s.storage_location,
        location_id=s.location_id,
        purchase_location=s.purchase_location,
        archived_at=s.archived_at,
        created_at=s.created_at,
        updated_at=s.updated_at,
        k_profile_count=len(s.k_profiles),
        k_profiles=([SpoolKProfileResponse.model_validate(kp) for kp in s.k_profiles] if include_k_profiles else None),
    )


class SpoolGroupItem(BaseModel):
    """One row of the grouped list mode (``group_similar=true``, task 3): the
    7-column group key + membership + the min(id) member as representative
    (slim list projection — same ``SpoolListItem`` the flat paged mode
    returns). The text key fields carry the COALESCED key value (``''`` where
    the underlying column is NULL — the client key's ``|| ''`` fold); ``lot``
    stays raw (``?? ''`` semantics: the all-NULL-lots group reports null,
    lot=0 is its own group)."""

    material: str
    subtype: str
    brand: str
    color_name: str
    rgba: str
    label_weight: int
    lot: int | None
    group_count: int
    ids: list[int]
    representative: SpoolListItem


class SpoolGroupPage(BaseModel):
    items: list[SpoolGroupItem]
    meta: PaginationMeta


@router.get("/spools", response_model=None)
async def list_spools(
    include_archived: bool = False,
    archived: str | None = Query(None, description="'active' or 'archived' — paged mode only"),
    usage: str | None = Query(None, description="'used', 'new', or 'lowstock'"),
    material: str | None = Query(None),
    brand: str | None = Query(None),
    colors: list[str] = Query(
        default_factory=list,
        description="Raw color_name values (resolved-name matching stays client-side — see facets, task 2)",
    ),
    color_rgbas: list[str] = Query(default_factory=list, description="Raw rgba values, paired with a NULL color_name"),
    category: str | None = Query(None, description="Exact match, or '__none__' for uncategorised"),
    catalog_id: int | None = Query(None),
    location_id: str | None = Query(
        None,
        # A stale/hand-edited deep-link is exactly the shape this endpoint
        # bakes into a shareable URL, so this must never be a 500 — see
        # _LOCATION_ID_PATTERN's comment (review finding 3).
        pattern=_LOCATION_ID_PATTERN,
        description="Location id, or '__none__' for no location",
    ),
    stock: str | None = Query(None, description="'stock' or 'configured'"),
    assigned: str | None = Query(None, description="'assigned' or 'unassigned'"),
    q: str | None = Query(
        None, description="Tokenised match over brand/material/color_name/subtype/note/slicer_filament_name"
    ),
    sort_by: str | None = Query(
        None,
        description=(
            "<column>_asc|_desc — see inventory_service._spool_sort_columns plus "
            "the special-cased 'display_name' and 'location' keys. Omitted keeps "
            "the legacy material/brand/color_name ordering."
        ),
    ),
    page: int | None = Query(None, ge=1, description="Omit entirely for the legacy flat-array response"),
    per_page: int = Query(50, ge=1, le=200),
    all: bool = Query(False, description="With page set, skip pagination and return every matching row"),
    group_similar: bool = Query(
        False,
        description=(
            "Paged mode only: rows become GROUPS of similar spools "
            "(material|subtype|brand|color_name|rgba|label_weight|lot) — "
            "see SpoolGroupItem. Requires page; restricts sort_by to the "
            "group-key subset (400 otherwise)."
        ),
    ),
    include_k_profiles: bool = Query(
        False,
        description=(
            "Paged mode only: serialize the full k_profiles array on each "
            "row (and on grouped representatives) instead of null — the "
            "cards-view opt-in (task 4). Serialization-only: the rows are "
            "eager-loaded either way."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
) -> list[SpoolResponse] | SpoolListPage | SpoolGroupPage:
    """List spools.

    Server-driven list (task 1, 2026-08-29 — mirrors ``ArchiveService.
    list_archives`` / the library file list): every filter param feeds
    ``inventory_service.build_spool_filters``, the SAME list driving both the
    page query and ``meta.total``. ``page`` is the compat switch — omit it
    (and every param below except ``include_archived``) and the response
    stays today's flat ``list[SpoolResponse]`` (full shape, ``k_profiles``
    included) — every existing caller (4 other frontend components, the
    Cloud Link remote op) depends on exactly that shape and never sends
    ``page``. Pass ``page`` and the response becomes
    ``{"items": [...], "meta": {total, current_page, per_page, last_page}}``
    of the slimmer ``SpoolListItem`` (see its docstring for what's dropped).

    ⚠️ **``include_archived`` is read ONLY on the legacy (no-``page``) branch —
    the paged branch ignores it entirely and relies on the new ``archived``
    param instead.** The deleted client's Active/Archived tab was strictly
    binary (never both at once); omitting ``archived`` in paged mode is,
    correctly per this endpoint's general "omit a param, get no filter for
    that dimension" contract, "show both" — not "active only" like the old
    default. A caller migrating to the paged mode (Task 4) must always send
    ``archived=active`` or ``archived=archived`` explicitly; sending
    ``page=1&include_archived=false`` and expecting the archived rows to stay
    hidden is a silent no-op (review finding 6).

    **Grouped mode (task 3):** ``group_similar=true`` (paged mode only —
    without ``page`` it's a 400, never a silent flat-shaped answer) makes the
    rows ``SpoolGroupItem`` GROUPS under the SAME filters: filters first,
    then grouping, then pagination over GROUPS — ``meta.total`` counts
    groups. The key and the merge-eligibility rule (used/assigned spools
    never merge) port the deleted client's ``spoolGroupKey`` + consumers
    exactly — see ``inventory_service._spool_group_key_exprs``. ``sort_by``
    is restricted to the group-key subset (``display_name``, ``material``,
    ``brand``, ``color_name`` — 400 otherwise, deliberately stricter than
    the flat mode's permissive fallback).
    """
    if page is None:
        if group_similar:
            raise HTTPException(
                status_code=400,
                detail="group_similar requires the paged mode — send page (grouped rows only exist in the envelope)",
            )
        spools = await inventory_service.list_spools(db, include_archived=include_archived)
        return [SpoolResponse.model_validate(s) for s in spools]

    if group_similar:
        # Fail fast, before any DB work — the service re-checks for direct
        # callers (same defense-in-depth as build_spool_filters' ValueError
        # contract on location_id).
        try:
            inventory_service.assert_group_sort_supported(sort_by)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    filters = await inventory_service.build_spool_filters(
        db,
        archived=archived,
        usage=usage,
        material=material,
        brand=brand,
        colors=colors or None,
        color_rgbas=color_rgbas or None,
        category=category,
        catalog_id=catalog_id,
        location_id=location_id,
        stock=stock,
        assigned=assigned,
        q=q,
    )

    offset, limit = (0, None) if all else ((page - 1) * per_page, per_page)

    if group_similar:
        total = await inventory_service.count_spool_groups(db, filters=filters)
        groups = await inventory_service.list_spool_groups(
            db, filters=filters, sort_by=sort_by, limit=limit, offset=offset
        )
        return SpoolGroupPage(
            items=[
                SpoolGroupItem(
                    material=g["material"],
                    subtype=g["subtype"],
                    brand=g["brand"],
                    color_name=g["color_name"],
                    rgba=g["rgba"],
                    label_weight=g["label_weight"],
                    lot=g["lot"],
                    group_count=g["group_count"],
                    ids=g["ids"],
                    representative=_spool_to_list_item(g["representative"], include_k_profiles=include_k_profiles),
                )
                for g in groups
            ],
            meta=PaginationMeta(
                total=total,
                current_page=1 if all else page,
                per_page=(total or 1) if all else per_page,
                last_page=1 if all else max(1, math.ceil(total / per_page)),
            ),
        )

    total = await inventory_service.count_spools(db, filters=filters)
    spools = await inventory_service.list_spools(db, filters=filters, sort_by=sort_by, limit=limit, offset=offset)

    last_page = 1 if all else max(1, math.ceil(total / per_page))
    return SpoolListPage(
        items=[_spool_to_list_item(s, include_k_profiles=include_k_profiles) for s in spools],
        meta=PaginationMeta(
            total=total,
            current_page=1 if all else page,
            per_page=(total or 1) if all else per_page,
            last_page=last_page,
        ),
    )


# ── Ids + facets — server-driven selection feeds (task 2, 2026-08-29) ────────
# Declared (like /spools/export below) before the dynamic `/spools/{spool_id}`
# route so the literal `ids` / `facets` segments match here instead of being
# parsed as an int id.


class SpoolIdsResponse(BaseModel):
    ids: list[int]


class SpoolColorFacet(BaseModel):
    color_name: str | None
    rgba: str | None


class SpoolFacetsResponse(BaseModel):
    materials: list[str]
    brands: list[str]
    categories: list[str]
    catalog_ids: list[int]
    colors: list[SpoolColorFacet]


@router.get("/spools/ids", response_model=SpoolIdsResponse)
async def list_spool_ids(
    archived: str | None = Query(None, description="'active' or 'archived'"),
    usage: str | None = Query(None, description="'used', 'new', or 'lowstock'"),
    material: str | None = Query(None),
    brand: str | None = Query(None),
    colors: list[str] = Query(default_factory=list, description="Raw color_name values"),
    color_rgbas: list[str] = Query(default_factory=list, description="Raw rgba values, paired with a NULL color_name"),
    category: str | None = Query(None, description="Exact match, or '__none__' for uncategorised"),
    catalog_id: int | None = Query(None),
    location_id: str | None = Query(
        None,
        pattern=_LOCATION_ID_PATTERN,
        description="Location id, or '__none__' for no location",
    ),
    stock: str | None = Query(None, description="'stock' or 'configured'"),
    assigned: str | None = Query(None, description="'assigned' or 'unassigned'"),
    q: str | None = Query(
        None, description="Tokenised match over brand/material/color_name/subtype/note/slicer_filament_name"
    ),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
) -> SpoolIdsResponse:
    """The ids of every spool matching the given filters — the "Select all N
    matching the filter" feed (spec §3.4).

    Takes the SAME filter + ``q`` params as the paged ``GET /spools`` (both
    feed ``inventory_service.build_spool_filters``, so the id set is exactly
    the rows the list shows), no paging/sort. The client materializes the
    answer into an explicit selection id set — bulk actions stay
    selection-scoped, never filter-scoped (the CLAUDE.md invariant). Refuses
    with 400 when more than ``_SPOOL_IDS_CAP`` rows match.
    """
    filters = await inventory_service.build_spool_filters(
        db,
        archived=archived,
        usage=usage,
        material=material,
        brand=brand,
        colors=colors or None,
        color_rgbas=color_rgbas or None,
        category=category,
        catalog_id=catalog_id,
        location_id=location_id,
        stock=stock,
        assigned=assigned,
        q=q,
    )
    ids = await inventory_service.list_spool_ids(db, filters=filters, limit=_SPOOL_IDS_CAP + 1)
    if len(ids) > _SPOOL_IDS_CAP:
        raise HTTPException(
            status_code=400,
            detail=f"More than {_SPOOL_IDS_CAP} spools match — narrow the filter before selecting all",
        )
    return SpoolIdsResponse(ids=ids)


@router.get("/spools/facets", response_model=SpoolFacetsResponse)
async def spool_facets(
    archived: str | None = Query(None, description="'active' or 'archived' — scope the facets to one tab"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
) -> SpoolFacetsResponse:
    """Distinct filter-dropdown values under the active archived tab (spec
    §3.6) — the server-driven replacement for deriving dropdown options from
    the full client-side array.

    Carries ONLY the dimensions with no existing source: materials, brands,
    categories, used catalog ids, and RAW ``(color_name, rgba)`` pairs (the
    client resolves and groups those by display name — the colour catalog
    lives client-side in ``ColorCatalogProvider``). Locations deliberately
    absent: the dropdown already reads ``GET /inventory/locations``.
    """
    filters = await inventory_service.build_spool_filters(db, archived=archived)
    facets = await inventory_service.spool_facets(db, filters=filters)
    return SpoolFacetsResponse(**facets)


# ── CSV import / export (#1576) ──────────────────────────────────────────────
# Declared before the dynamic `/spools/{spool_id}` route below so the literal
# `export` / `import` segments match here instead of being parsed as an int id.
# Bounded read size for the CSV import body so a chunked upload with no
# Content-Length can't stream past the cap into memory before we notice.
_CSV_UPLOAD_CHUNK_BYTES = 64 * 1024


@router.get("/spools/export")
async def export_spools_csv(
    delimiter: Literal["comma", "semicolon", "tab"] = Query("comma"),
    decimal: Literal["dot", "comma"] = Query("dot"),
    encoding: Literal["utf-8", "utf-8-bom"] = Query("utf-8"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """Export the active inventory as CSV (same schema the importer accepts).

    The locale knobs exist because a spreadsheet is usually the next stop:
    a European locale wants ``;`` cells and ``,`` decimals to see columns and
    numbers, and Windows Excel needs the BOM to read UTF-8 at all.
    """
    from datetime import datetime, timezone

    query = select(Spool).where(Spool.archived_at.is_(None)).order_by(Spool.material, Spool.brand, Spool.color_name)
    result = await db.execute(query)
    spools = list(result.scalars().all())
    content = serialize(spools, delimiter=delimiter, decimal=decimal, bom=encoding == "utf-8-bom")
    # Date-stamp the filename so repeat exports don't overwrite each other in
    # the browser's default download folder.
    filename = f"bamdude_inventory_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/spools/import", response_model=ImportPreview | ImportResult)
async def import_spools_csv(
    file: UploadFile = File(...),
    dry_run: bool = Query(False),
    encoding: Literal["auto", "utf-8", "windows-1251", "windows-1252"] = Query("auto"),
    delimiter: Literal["auto", "comma", "semicolon", "tab"] = Query("auto"),
    decimal: Literal["auto", "dot", "comma"] = Query("auto"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Import spools from a CSV file.

    With ``dry_run=true`` returns an ImportPreview (per-row valid/error/skipped,
    colours resolved) and writes nothing — the UI shows this before the user
    confirms. With ``dry_run=false`` it validates the same way and then persists
    only the valid rows in a single transaction (invalid rows are skipped, the
    user fixes the CSV and re-uploads), returning an ImportResult summary.
    """

    def _too_large() -> HTTPException:
        return HTTPException(
            status_code=413,
            detail={
                "code": "csv_import_too_large",
                "message": f"CSV file exceeds the {MAX_CSV_IMPORT_BYTES // (1024 * 1024)} MB limit.",
            },
        )

    # Reject by declared size first (fast path when Content-Length is set), then
    # read in bounded chunks and bail the moment the accumulated body crosses the
    # cap — file.size is None for chunked uploads, so the loop is what actually
    # keeps an oversized stream from filling memory.
    if file.size is not None and file.size > MAX_CSV_IMPORT_BYTES:
        raise _too_large()
    raw = bytearray()
    while chunk := await file.read(_CSV_UPLOAD_CHUNK_BYTES):
        raw.extend(chunk)
        if len(raw) > MAX_CSV_IMPORT_BYTES:
            raise _too_large()
    preview = await parse_and_validate(bytes(raw), db, encoding=encoding, delimiter=delimiter, decimal=decimal)

    if dry_run:
        return preview

    created = 0
    for row in preview.rows:
        if row.status == "valid" and row.spool is not None:
            db.add(Spool(**row.spool))
            created += 1

    if created:
        await db.commit()
        await ws_manager.broadcast({"type": "inventory_changed"})

    return ImportResult(
        created=created,
        skipped=preview.skipped_count,
        errors=preview.error_count,
        error_rows=[r for r in preview.rows if r.status == "error"],
    )


@router.get("/spools/by-tag", response_model=SpoolResponse)
async def lookup_spool_by_tag(
    tray_uuid: str | None = None,
    tag_uid: str | None = None,
    include_archived: bool = False,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAnyPermission(Permission.INVENTORY_READ, Permission.INVENTORY_UPDATE),
):
    """Find a single spool by its NFC ``tray_uuid`` and/or ``tag_uid``.

    Lets NFC inventory integrations dedupe a scan without listing the whole
    inventory. ``tray_uuid`` is the primary identifier; ``tag_uid`` is the
    fallback. At least one must be supplied. Returns 404 when nothing matches.

    Accepts ``inventory:read`` OR ``inventory:update`` so a Manage-Inventory API
    key (``inventory:update``) can read a spool back without widening the global
    read scope (upstream Bambuddy #1663). Declared before ``/spools/{spool_id}``
    so the literal path isn't captured by the integer param route.
    """
    normalized_tray_uuid = normalize_tray_uuid(tray_uuid) or None
    normalized_tag_uid = normalize_tag_uid(tag_uid) or None
    if not normalized_tray_uuid and not normalized_tag_uid:
        raise HTTPException(400, "Provide tray_uuid and/or tag_uid")
    base_query = select(Spool).options(selectinload(Spool.k_profiles))
    if not include_archived:
        base_query = base_query.where(Spool.archived_at.is_(None))
    for column, value in ((Spool.tray_uuid, normalized_tray_uuid), (Spool.tag_uid, normalized_tag_uid)):
        if not value:
            continue
        result = await db.execute(base_query.where(func.upper(column) == value).order_by(Spool.id))
        spool = result.scalars().first()
        if spool:
            return spool
    raise HTTPException(404, "Spool not found")


@router.get("/spools/{spool_id}", response_model=SpoolResponse)
async def get_spool(
    spool_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """Get a single spool with k_profiles."""
    result = await db.execute(select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id == spool_id))
    spool = result.scalar_one_or_none()
    if not spool:
        raise HTTPException(404, "Spool not found")
    return spool


@router.post("/spools", response_model=SpoolResponse)
async def create_spool(
    spool_data: SpoolCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Create a new spool."""
    await _validate_family_id(db, spool_data.filament_family_id)
    try:
        payload = await prepare_internal_spool_payload(db, spool_data.model_dump(), set(spool_data.model_fields_set))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    spool = Spool(**payload)
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    await _safe_autolink(db, spool)
    result = await db.execute(select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id == spool.id))
    return result.scalar_one()


@router.post("/spools/bulk", response_model=list[SpoolResponse])
async def bulk_create_spools(
    data: SpoolBulkCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Create multiple identical spools.

    With ``auto_increment_lot=True`` the per-row ``lot`` column is
    sequenced from the template's ``lot`` value (the number the user
    entered) instead of copying it unchanged — e.g. a bundle starting at
    lot 5 gets 5, 6, 7. Falls back to starting at 1 when no lot was given,
    so a purchase bundle gets sequential lot numbers in one submit.
    """
    spools = []
    try:
        template = await prepare_internal_spool_payload(db, data.spool.model_dump(), set(data.spool.model_fields_set))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    base_lot = template.get("lot")
    start_lot = base_lot if isinstance(base_lot, int) and base_lot > 0 else 1
    for i in range(data.quantity):
        values = dict(template)
        if data.auto_increment_lot:
            values["lot"] = start_lot + i
        spool = Spool(**values)
        db.add(spool)
        spools.append(spool)
    await db.commit()
    for spool in spools:
        await _safe_autolink(db, spool)
    ids = [s.id for s in spools]
    result = await db.execute(select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id.in_(ids)))
    return list(result.scalars().all())


@router.patch("/spools/bulk-update", response_model=list[SpoolResponse])
async def bulk_update_spools(
    data: SpoolBulkUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Apply the same partial field set to many spools at once.

    Only fields the caller explicitly sent are written (others are left as-is
    per spool). Usage and per-physical-spool identity columns are never
    bulk-set: consumed weight stays per-spool, and an RFID UID can't be copied
    across spools. Declared before ``/spools/{spool_id}`` so the literal path
    isn't captured by the int ``spool_id`` matcher.
    """
    update_data = data.fields.model_dump(exclude_unset=True)
    for protected in ("weight_used", "weight_used_baseline", "weight_locked", "tag_uid", "tray_uuid"):
        update_data.pop(protected, None)
    if not update_data:
        raise HTTPException(400, "No editable fields provided")
    # Resolve location_id / storage_location into the FK-alongside-free-text
    # pair (upstream #1505). A free-text bulk edit creates/links the catalog
    # row; location_id wins when both are present.
    try:
        update_data = await prepare_internal_spool_payload(db, update_data, set(data.fields.model_fields_set))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result = await db.execute(select(Spool).where(Spool.id.in_(data.spool_ids)))
    spools = list(result.scalars().all())
    if not spools:
        raise HTTPException(404, "No spools found")

    await _validate_family_id(db, update_data.get("filament_family_id"))
    for spool in spools:
        for field, value in update_data.items():
            setattr(spool, field, value)
    await db.commit()

    if "filament_family_id" in update_data or "slicer_filament" in update_data:
        for spool in spools:
            await _safe_autolink(db, spool)

    ids = [s.id for s in spools]
    result = await db.execute(select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id.in_(ids)))
    return list(result.scalars().all())


@router.post("/spools/bulk-delete")
async def bulk_delete_spools(
    data: SpoolBulkIds,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Hard-delete every listed spool.

    Ids that no longer exist are reported in ``not_found`` rather than failing
    the batch — another tab may have deleted one between the click and the
    request, and aborting would leave the rest of the user's selection
    untouched with no indication of how far it got. Declared before
    ``/spools/{spool_id}`` so the literal path isn't captured by the int
    matcher.
    """
    result = await db.execute(select(Spool).where(Spool.id.in_(data.spool_ids)))
    spools = list(result.scalars().all())
    found_ids = {s.id for s in spools}
    not_found = [i for i in data.spool_ids if i not in found_ids]

    for spool in spools:
        await db.delete(spool)
    await db.commit()

    # One broadcast for the whole batch, not one per row: the table refreshes
    # on a single re-fetch instead of N.
    await ws_manager.broadcast({"type": "inventory_changed"})
    return {"deleted": len(spools), "not_found": not_found}


@router.post("/spools/bulk-archive")
async def bulk_archive_spools(
    data: SpoolBulkIds,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Soft-archive every listed spool (sets ``archived_at``).

    Already-archived spools are left alone and counted separately so the UI can
    tell "nothing to do" apart from "that row is gone".
    """
    from datetime import datetime, timezone

    result = await db.execute(select(Spool).where(Spool.id.in_(data.spool_ids)))
    spools = list(result.scalars().all())
    found_ids = {s.id for s in spools}
    not_found = [i for i in data.spool_ids if i not in found_ids]

    now = datetime.now(timezone.utc)
    archived: list[int] = []
    already: list[int] = []
    for spool in spools:
        if spool.archived_at is not None:
            already.append(spool.id)
            continue
        spool.archived_at = now
        archived.append(spool.id)
    await db.commit()

    await ws_manager.broadcast({"type": "inventory_changed"})
    return {"archived": len(archived), "already_archived": len(already), "not_found": not_found}


@router.post("/spools/bulk-restore")
async def bulk_restore_spools(
    data: SpoolBulkIds,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Un-archive every listed spool — the symmetric inverse of bulk-archive."""
    result = await db.execute(select(Spool).where(Spool.id.in_(data.spool_ids)))
    spools = list(result.scalars().all())
    found_ids = {s.id for s in spools}
    not_found = [i for i in data.spool_ids if i not in found_ids]

    restored: list[int] = []
    already: list[int] = []
    for spool in spools:
        if spool.archived_at is None:
            already.append(spool.id)
            continue
        spool.archived_at = None
        restored.append(spool.id)
    await db.commit()

    await ws_manager.broadcast({"type": "inventory_changed"})
    return {"restored": len(restored), "already_active": len(already), "not_found": not_found}


@router.patch("/spools/{spool_id}", response_model=SpoolResponse)
async def update_spool(
    spool_id: int,
    spool_data: SpoolUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Update a spool."""
    try:
        return await inventory_service.update_spool(db, spool_id, spool_data)
    except inventory_service.SpoolNotFoundError:
        raise HTTPException(404, "Spool not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/spools/{spool_id}/relink-kprofiles")
async def relink_spool_kprofiles(
    spool_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Manually re-run K-profile auto-linking for one spool."""
    from backend.app.services.kprofile_autolink import autolink_spool

    result = await db.execute(select(Spool).where(Spool.id == spool_id))
    spool = result.scalar_one_or_none()
    if not spool:
        raise HTTPException(404, "Spool not found")
    count = await autolink_spool(db=db, spool=spool)
    await db.commit()
    return {"status": "ok", "linked": count}


@router.delete("/spools/{spool_id}")
async def delete_spool(
    spool_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Hard delete a spool."""
    result = await db.execute(select(Spool).where(Spool.id == spool_id))
    spool = result.scalar_one_or_none()
    if not spool:
        raise HTTPException(404, "Spool not found")

    await db.delete(spool)
    await db.commit()
    return {"status": "deleted"}


@router.post("/spools/{spool_id}/archive", response_model=SpoolResponse)
async def archive_spool(
    spool_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Soft-delete a spool by setting archived_at."""
    from datetime import datetime, timezone

    result = await db.execute(select(Spool).where(Spool.id == spool_id))
    spool = result.scalar_one_or_none()
    if not spool:
        raise HTTPException(404, "Spool not found")

    spool.archived_at = datetime.now(timezone.utc)
    await db.commit()
    result = await db.execute(select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id == spool_id))
    return result.scalar_one()


@router.post("/spools/{spool_id}/restore", response_model=SpoolResponse)
async def restore_spool(
    spool_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Restore an archived spool."""
    result = await db.execute(select(Spool).where(Spool.id == spool_id))
    spool = result.scalar_one_or_none()
    if not spool:
        raise HTTPException(404, "Spool not found")

    spool.archived_at = None
    await db.commit()
    result = await db.execute(select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id == spool_id))
    return result.scalar_one()


@router.post("/spools/{spool_id}/reset-consumed-counter", response_model=SpoolResponse)
async def reset_spool_consumed_counter(
    spool_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Zero the displayed "Total Consumed" counter without touching remaining.

    Renamed from ``/reset-usage`` (#1644): the old name implied it would drop
    ``weight_used`` to 0, but it only stamps the baseline so the "Total Consumed"
    widget reads 0 going forward — ``weight_used`` (and remaining) are unchanged.

    Stamps ``weight_used_baseline = weight_used`` so the Inventory page's
    ``max(0, weight_used - baseline)`` display reads 0, while
    ``label_weight - weight_used`` (remaining) is unchanged. ``weight_locked``
    is also left alone — the spool keeps receiving AMS auto-sync updates
    from the next print onward. Mirrors Spoolman's split between
    ``used_weight`` and ``remaining_weight`` (upstream Bambuddy #1390).
    """
    result = await db.execute(select(Spool).where(Spool.id == spool_id))
    spool = result.scalar_one_or_none()
    if not spool:
        raise HTTPException(404, "Spool not found")

    spool.weight_used_baseline = spool.weight_used or 0
    await db.commit()
    result = await db.execute(select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id == spool_id))
    await ws_manager.broadcast({"type": "inventory_changed"})
    return result.scalar_one()


@router.post("/spools/reset-consumed-counter-bulk")
async def bulk_reset_spool_consumed_counter(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Bulk-stamp ``baseline = weight_used`` across the given spool IDs.

    Caller passes an explicit list of IDs — no "reset all" shortcut, since
    a typo on a wildcard would wipe the entire inventory's tracking. Same
    semantics as the per-spool endpoint: remaining is preserved,
    ``weight_locked`` is left alone (upstream Bambuddy #1390).
    """
    spool_ids = payload.get("spool_ids")
    if not isinstance(spool_ids, list) or not spool_ids:
        raise HTTPException(400, "spool_ids must be a non-empty list")
    if not all(isinstance(sid, int) for sid in spool_ids):
        raise HTTPException(400, "spool_ids must contain integers")

    result = await db.execute(select(Spool).where(Spool.id.in_(spool_ids)))
    spools = list(result.scalars().all())
    for spool in spools:
        spool.weight_used_baseline = spool.weight_used or 0
    await db.commit()
    await ws_manager.broadcast({"type": "inventory_changed"})
    return {"reset": len(spools)}


# ── K-Profiles ───────────────────────────────────────────────────────────────


@router.get("/spools/{spool_id}/k-profiles", response_model=list[SpoolKProfileResponse])
async def list_k_profiles(
    spool_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """List K-profiles for a spool."""
    result = await db.execute(select(SpoolKProfile).where(SpoolKProfile.spool_id == spool_id))
    return list(result.scalars().all())


@router.put("/spools/{spool_id}/k-profiles", response_model=list[SpoolKProfileResponse])
async def replace_k_profiles(
    spool_id: int,
    profiles: list[SpoolKProfileBase],
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Replace all K-profile links for a spool (batch save).

    Wire shape (``SpoolKProfileBase``) is unchanged for the PA tab. For each
    incoming entry the backend resolves the printer's live ``cali_idx`` →
    find-or-create a ``filament_calibration`` cache row → create the link.
    """
    if not (await db.execute(select(Spool).where(Spool.id == spool_id))).scalar_one_or_none():
        raise HTTPException(404, "Spool not found")

    existing = await db.execute(select(SpoolKProfile).where(SpoolKProfile.spool_id == spool_id))
    for old in existing.scalars().all():
        await db.delete(old)

    new_links: list[SpoolKProfile] = []
    for p in profiles:
        fc = await _find_or_create_filament_calibration_for_link(db, p)
        if fc is None:
            continue
        link = SpoolKProfile(
            spool_id=spool_id,
            printer_id=p.printer_id,
            extruder=p.extruder,
            filament_calibration_id=fc.id,
        )
        db.add(link)
        new_links.append(link)

    await db.commit()
    for link in new_links:
        await db.refresh(link)
    return new_links


async def _find_or_create_filament_calibration_for_link(db: AsyncSession, p: SpoolKProfileBase):
    """Resolve a PA-tab entry to a ``filament_calibration`` cache row.

    Strategy: pull full identity from the printer's live K-profile list by
    the user-picked ``cali_idx`` (which is fresh at the moment of click).
    Find existing cache row by stable combo + EXACT K, create if absent
    (matches the m064 backfill pattern — new rows ship ``is_active=False``).
    """
    from backend.app.models.filament_calibration import FilamentCalibration
    from backend.app.services.calibration_service import parse_nozzle_vol_type
    from backend.app.services.printer_manager import printer_manager

    client = printer_manager.get_client(p.printer_id)
    if not client or not client.state.connected or p.cali_idx is None:
        return None

    target_kp = None
    for kp in client.state.kprofiles or []:
        try:
            slot = int(kp.slot_id)
        except (TypeError, ValueError):
            continue
        if slot == int(p.cali_idx):
            target_kp = kp
            break
    if target_kp is None:
        return None

    try:
        kp_k = float(target_kp.k_value)
    except (TypeError, ValueError):
        return None
    try:
        nozzle_dia = float(target_kp.nozzle_diameter or p.nozzle_diameter or 0.4)
    except (TypeError, ValueError):
        nozzle_dia = 0.4
    nozzle_vt = parse_nozzle_vol_type(getattr(target_kp, "nozzle_id", None) or p.nozzle_type)
    filament_id = target_kp.filament_id or ""
    display_name = target_kp.name or p.name or f"{filament_id} K={kp_k:.4f}"

    existing = (
        (
            await db.execute(
                select(FilamentCalibration).where(
                    FilamentCalibration.printer_id == p.printer_id,
                    FilamentCalibration.filament_id == filament_id,
                    FilamentCalibration.nozzle_diameter == nozzle_dia,
                    FilamentCalibration.nozzle_volume_type == nozzle_vt,
                    FilamentCalibration.extruder_id == p.extruder,
                    FilamentCalibration.pa_k_value == kp_k,
                )
            )
        )
        .scalars()
        .all()
    )
    active = next((r for r in existing if r.is_active), None)
    if active:
        return active
    if existing:
        return existing[0]

    new_row = FilamentCalibration(
        printer_id=p.printer_id,
        filament_id=filament_id,
        filament_setting_id=target_kp.setting_id or p.setting_id,
        nozzle_diameter=nozzle_dia,
        nozzle_volume_type=nozzle_vt,
        extruder_id=p.extruder,
        pa_k_value=kp_k,
        cali_mode="pa_line",
        source="spool_link",
        is_active=False,
        cali_idx=int(target_kp.slot_id) if target_kp.slot_id is not None else None,
        name=display_name,
    )
    db.add(new_row)
    await db.flush()
    return new_row


# ── Spool Assignments ────────────────────────────────────────────────────────


@router.get("/assignments/replacement-window/{printer_id}")
async def get_replacement_window(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_VIEW_ASSIGNMENTS),
):
    """How the assign dialog should treat a mid-print assignment right now.

    ``prompt`` — paused: ask "replacement or correction?" before assigning.
    ``optin`` — running, but this print has a pause behind it: offer a
    default-off checkbox (the swap, if any, happened at that pause).
    ``none`` — no active print or never paused: a physical replacement is
    impossible, plain assignment (wrong-link correction) with no friction.
    """
    from backend.app.services.print_usage_journal import manual_replacement_window

    window = await manual_replacement_window(db, printer_id)
    if window is None:
        return {"mode": "none", "pause_layer": None}
    return {"mode": window["mode"], "pause_layer": window["pause_layer"]}


@router.get("/assignments", response_model=list[SpoolAssignmentResponse])
async def list_assignments(
    printer_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_VIEW_ASSIGNMENTS),
):
    """List spool assignments, optionally filtered by printer."""
    from backend.app.services.printer_manager import printer_manager

    query = select(SpoolAssignment).options(
        selectinload(SpoolAssignment.spool).selectinload(Spool.k_profiles),
        selectinload(SpoolAssignment.printer),
    )
    if printer_id is not None:
        query = query.where(SpoolAssignment.printer_id == printer_id)
    result = await db.execute(query)
    assignments = list(result.scalars().all())

    # Build (printer_id, ams_id) -> ams_serial map from live printer states.
    # Fetch all statuses in one call rather than one get_status() call per printer.
    serial_map: dict[tuple[int, int], str] = {}
    seen_printer_ids: set[int] = {a.printer_id for a in assignments}
    all_statuses = printer_manager.get_all_statuses()
    for pid in seen_printer_ids:
        state = all_statuses.get(pid)
        if state and state.raw_data:
            for ams_unit in state.raw_data.get("ams", []):
                sn = str(ams_unit.get("sn") or ams_unit.get("serial_number") or "")
                if sn:
                    try:
                        serial_map[(pid, int(ams_unit.get("id", 0)))] = sn
                    except (ValueError, TypeError):
                        continue

    # Fetch all relevant AMS labels keyed by serial number
    all_serials = set(serial_map.values())
    # Also include synthetic fallback keys for assignments without a known serial
    synthetic_keys: dict[str, tuple[int, int]] = {}
    for a in assignments:
        if (a.printer_id, a.ams_id) not in serial_map:
            synthetic = f"p{a.printer_id}a{a.ams_id}"
            synthetic_keys[synthetic] = (a.printer_id, a.ams_id)
            all_serials.add(synthetic)

    label_by_serial: dict[str, str] = {}
    if all_serials:
        lbl_result = await db.execute(select(AmsLabel).where(AmsLabel.ams_serial_number.in_(all_serials)))
        for lbl in lbl_result.scalars().all():
            label_by_serial[lbl.ams_serial_number] = lbl.label

    # Build response objects, attaching ams_label where available
    responses: list[SpoolAssignmentResponse] = []
    for a in assignments:
        resp = SpoolAssignmentResponse.model_validate(a)
        sn = serial_map.get((a.printer_id, a.ams_id))
        if sn and sn in label_by_serial:
            resp.ams_label = label_by_serial[sn]
        elif not sn:
            synthetic = f"p{a.printer_id}a{a.ams_id}"
            resp.ams_label = label_by_serial.get(synthetic)
        responses.append(resp)

    return responses


@router.post("/assignments", response_model=SpoolAssignmentResponse)
async def assign_spool(
    data: SpoolAssignmentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Assign a spool to an AMS slot and auto-configure via MQTT."""
    from backend.app.services.printer_manager import printer_manager

    # 1. Validate spool exists and is not archived
    result = await db.execute(select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id == data.spool_id))
    spool = result.scalar_one_or_none()
    if not spool:
        raise HTTPException(404, "Spool not found")
    if spool.archived_at:
        raise HTTPException(400, "Cannot assign an archived spool")

    # 2. Get current AMS tray state for fingerprint + existing filament ID.
    #
    # ``tray_state`` (#1322 follow-up): Bambu firmware reports 11 = loaded,
    # 9 = empty, 10 = spool present but filament not in feeder. Captured here
    # so the empty-slot guard below can prefer the firmware's explicit
    # signals over the stale-tray_type heuristic — a manual "Reset Slot"
    # clears ``tray_type`` to "" while leaving the spool physically present,
    # which used to mislead the heuristic into the pending-config branch
    # and skip MQTT forever (upstream Bambuddy f45aaea9 / dca05ce6 /
    # 7d3af983 / e2df0fc6 — final state).
    fingerprint_color = None
    fingerprint_type = None
    current_tray_info_idx = ""
    tray_state: int | None = None
    state = printer_manager.get_status(data.printer_id)
    if state and state.raw_data:
        if data.ams_id == 255:
            # External slot: look up tray from vt_tray by global ID
            vt_tray = state.raw_data.get("vt_tray") or []
            ext_id = data.tray_id + 254  # 0→254, 1→255
            for vt in vt_tray:
                if isinstance(vt, dict) and int(vt.get("id", 254)) == ext_id:
                    fingerprint_color = vt.get("tray_color", "")
                    fingerprint_type = vt.get("tray_type", "")
                    current_tray_info_idx = vt.get("tray_info_idx", "")
                    raw_state = vt.get("state")
                    if isinstance(raw_state, int):
                        tray_state = raw_state
                    break
        else:
            ams_data = state.raw_data.get("ams", {})
            ams_list = (
                ams_data.get("ams", [])
                if isinstance(ams_data, dict)
                else ams_data
                if isinstance(ams_data, list)
                else []
            )
            tray = _find_tray_in_ams_data(
                ams_list,
                data.ams_id,
                data.tray_id,
            )
            if tray:
                fingerprint_color = tray.get("tray_color", "")
                fingerprint_type = tray.get("tray_type", "")
                current_tray_info_idx = tray.get("tray_info_idx", "")
                raw_state = tray.get("state")
                if isinstance(raw_state, int):
                    tray_state = raw_state

    # Deliberate mid-pause replacement (the user answered the "replacement or
    # correction?" prompt with "replacement"): journal the manual runout NOW,
    # while the outgoing spool is still the current assignment, so the
    # assignment below closes it as the spool_loaded boundary. Refusal is
    # loud — silently downgrading to a correction would misattribute.
    if data.mid_print_replacement:
        from backend.app.services.print_usage_journal import note_manual_replacement_intent

        if not await note_manual_replacement_intent(
            db, printer_id=data.printer_id, ams_id=data.ams_id, tray_id=data.tray_id
        ):
            raise HTTPException(409, "Mid-print replacement needs a print that is paused or has been paused")

    # 3. Upsert assignment (replace if same printer+ams+tray)
    existing = await db.execute(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == data.printer_id,
            SpoolAssignment.ams_id == data.ams_id,
            SpoolAssignment.tray_id == data.tray_id,
        )
    )
    old = existing.scalar_one_or_none()
    if old:
        await db.delete(old)
        await db.flush()

    assignment = SpoolAssignment(
        spool_id=data.spool_id,
        printer_id=data.printer_id,
        ams_id=data.ams_id,
        tray_id=data.tray_id,
        fingerprint_color=fingerprint_color,
        fingerprint_type=fingerprint_type,
    )
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)

    # If this slot ran out during the active print, this assignment IS the
    # replacement (the runout notification asks for exactly this) — journal
    # the spool_loaded boundary. Best-effort; the assignment must never fail
    # on bookkeeping.
    try:
        from backend.app.services.print_usage_journal import note_assignment_change

        await note_assignment_change(
            db,
            printer_id=data.printer_id,
            ams_id=data.ams_id,
            tray_id=data.tray_id,
            spool_id=data.spool_id,
        )
    except Exception:
        logger.exception("note_assignment_change failed for printer %s", data.printer_id)

    # re-Connect MQTT if stalled
    await printer_manager.ensure_fresh_connection(data.printer_id)

    # 4. Auto-configure AMS slot via MQTT.
    #
    # Suppress the publish ONLY when firmware's *explicit* empty signal is
    # set — ``tray_state ∈ {9, 10}`` ("no spool" / "spool present but no
    # feed"). Every other state, including state=3 (the default idle on
    # A1 Mini BMCU / P1S Standard AMS for both loaded and unconfigured
    # slots) and missing state (older firmwares), is treated as "the user
    # asserts a spool is in this slot" and we attempt the MQTT push.
    #
    # The pre-existing "skip when tray_type is empty" heuristic was wrong
    # for the "Reset Slot on printer screen with the spool still inserted"
    # flow — on A1 Mini BMCU / P1S Standard AMS, that combination is
    # state=3 + tray_type="" with the spool physically present, and there
    # is NO AMS signal that distinguishes it from a truly-empty slot. The
    # heuristic created a deadlock: MQTT never fired, the AMS never
    # reported any change (because nothing physically changed), so the
    # ``on_ams_change`` replay never re-fired the config either. Bambu
    # firmware DOES accept the push for a physically-loaded slot with
    # tray_type="" + state=3, so removing the guard configures the slot
    # correctly (upstream Bambuddy #1322 / dca05ce6).
    #
    # Trade-off for the truly-empty case: firmware drops the push
    # silently, the ``SpoolAssignment`` row still has empty
    # ``fingerprint_type``, and ``on_ams_change`` still fires the deferred
    # config when a spool eventually appears — the weigh-then-assign
    # SpoolBuddy workflow keeps working, just without the optimisation of
    # skipping a no-op MQTT call.
    slot_is_definitely_empty = tray_state == 9 or tray_state == 10
    configured = False
    pending_config = slot_is_definitely_empty

    if slot_is_definitely_empty:
        logger.info(
            "Pre-configured assignment: spool %d → printer %d AMS%d-T%d (firmware reports empty state=%s, will configure on insert)",
            spool.id,
            data.printer_id,
            data.ams_id,
            data.tray_id,
            tray_state,
        )
    else:
        try:
            configured = await apply_spool_to_slot_via_mqtt(
                db=db,
                current_user=current_user,
                spool=spool,
                printer_id=data.printer_id,
                ams_id=data.ams_id,
                tray_id=data.tray_id,
                current_tray_info_idx=current_tray_info_idx,
                current_tray_type=fingerprint_type or "",
            )
        except Exception as e:
            logger.warning("MQTT auto-configure failed for spool %d: %s", spool.id, e)
        else:
            # Nudge a fresh pushall so the read-back verification registered in
            # apply_spool_to_slot_via_mqtt (upstream #2582) has current tray
            # telemetry to compare against within its window, instead of waiting
            # for the next idle push. Best-effort — the periodic push is the
            # fallback.
            if configured:
                try:
                    client = printer_manager.get_client(data.printer_id)
                    if client:
                        client.request_status_update()
                except Exception:
                    pass

    # Return assignment with spool data
    result = await db.execute(
        select(SpoolAssignment)
        .options(
            selectinload(SpoolAssignment.spool).selectinload(Spool.k_profiles),
            selectinload(SpoolAssignment.printer),
        )
        .where(SpoolAssignment.id == assignment.id)
    )
    resp = result.scalar_one()
    response = SpoolAssignmentResponse.model_validate(resp)
    response.configured = configured
    response.pending_config = pending_config

    await ws_manager.broadcast(
        {
            "type": "spool_assignment_changed",
            "printer_id": data.printer_id,
            "ams_id": data.ams_id,
            "tray_id": data.tray_id,
        }
    )

    return response


@router.delete("/assignments/{printer_id}/{ams_id}/{tray_id}")
async def unassign_spool(
    printer_id: int,
    ams_id: int,
    tray_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Unassign a spool from an AMS slot."""
    result = await db.execute(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    assignment = result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(404, "Assignment not found")

    await db.delete(assignment)
    await db.commit()

    await ws_manager.broadcast(
        {
            "type": "spool_assignment_changed",
            "printer_id": printer_id,
            "ams_id": ams_id,
            "tray_id": tray_id,
        }
    )

    return {"status": "deleted"}


# ── Tag Linking ───────────────────────────────────────────────────────────────


class LinkTagRequest(BaseModel):
    tag_uid: str | None = None
    tray_uuid: str | None = None
    tag_type: str | None = None
    data_origin: str | None = "nfc_link"


def _validate_tag_input(
    raw_value: str | None, normalized_value: str | None, field_name: str, exact_len: int | None = None
) -> None:
    if raw_value is None:
        return
    raw = str(raw_value).strip()
    if not raw:
        return
    if normalized_value is None:
        raise HTTPException(422, f"{field_name} must contain hexadecimal characters")
    if len(normalized_value) % 2 != 0:
        raise HTTPException(422, f"{field_name} must have an even number of hex characters")
    if exact_len is not None and len(normalized_value) != exact_len:
        raise HTTPException(422, f"{field_name} must be exactly {exact_len} hex characters")


@router.patch("/spools/{spool_id}/link-tag", response_model=SpoolResponse)
async def link_tag_to_spool(
    spool_id: int,
    data: LinkTagRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Link an RFID tag_uid/tray_uuid to an existing spool."""
    result = await db.execute(select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id == spool_id))
    spool = result.scalar_one_or_none()
    if not spool:
        raise HTTPException(404, "Spool not found")
    if spool.archived_at:
        raise HTTPException(400, "Cannot link tag to archived spool")

    normalized_tag_uid = (normalize_tag_uid(data.tag_uid) or None) if data.tag_uid is not None else None
    normalized_tray_uuid = (normalize_tray_uuid(data.tray_uuid) or None) if data.tray_uuid is not None else None

    _validate_tag_input(data.tag_uid, normalized_tag_uid, "tag_uid")
    _validate_tag_input(data.tray_uuid, normalized_tray_uuid, "tray_uuid", exact_len=32)

    # Check for conflicts: tag already linked to another active spool
    if normalized_tag_uid:
        conflict = await db.execute(
            select(Spool).where(
                func.upper(Spool.tag_uid) == normalized_tag_uid,
                Spool.id != spool_id,
                Spool.archived_at.is_(None),
            )
        )
        if conflict.scalar_one_or_none():
            raise HTTPException(409, "Tag UID already linked to another active spool")
        # Auto-clear from archived spools (tag recycling)
        archived_with_tag = await db.execute(
            select(Spool).where(
                func.upper(Spool.tag_uid) == normalized_tag_uid,
                Spool.id != spool_id,
                Spool.archived_at.is_not(None),
            )
        )
        for old_spool in archived_with_tag.scalars().all():
            old_spool.tag_uid = None

    if normalized_tray_uuid:
        conflict = await db.execute(
            select(Spool).where(
                func.upper(Spool.tray_uuid) == normalized_tray_uuid,
                Spool.id != spool_id,
                Spool.archived_at.is_(None),
            )
        )
        if conflict.scalar_one_or_none():
            raise HTTPException(409, "Tray UUID already linked to another active spool")
        archived_with_uuid = await db.execute(
            select(Spool).where(
                func.upper(Spool.tray_uuid) == normalized_tray_uuid,
                Spool.id != spool_id,
                Spool.archived_at.is_not(None),
            )
        )
        for old_spool in archived_with_uuid.scalars().all():
            old_spool.tray_uuid = None

    if data.tag_uid is not None:
        spool.tag_uid = normalized_tag_uid
    if data.tray_uuid is not None:
        spool.tray_uuid = normalized_tray_uuid
    if data.tag_type is not None:
        spool.tag_type = data.tag_type
    if data.data_origin is not None:
        spool.data_origin = data.data_origin

    await db.commit()
    result = await db.execute(select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id == spool_id))
    return result.scalar_one()


# ── Usage History ─────────────────────────────────────────────────────────────


@router.get("/spools/{spool_id}/usage", response_model=list[SpoolUsageHistoryResponse])
async def get_spool_usage_history(
    spool_id: int,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """Get usage history for a specific spool."""
    from backend.app.models.spool_usage_history import SpoolUsageHistory

    # Verify spool exists
    spool_result = await db.execute(select(Spool).where(Spool.id == spool_id))
    if not spool_result.scalar_one_or_none():
        raise HTTPException(404, "Spool not found")

    result = await db.execute(
        select(SpoolUsageHistory)
        .where(SpoolUsageHistory.spool_id == spool_id)
        # id breaks the tie: a runout close-out lands in the same second as
        # the print's own rows, and date alone shows them in random order
        .order_by(SpoolUsageHistory.created_at.desc(), SpoolUsageHistory.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


@router.get("/usage", response_model=list[SpoolUsageHistoryResponse])
async def get_all_usage_history(
    limit: int = 100,
    printer_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """Get global usage history, optionally filtered by printer."""
    from backend.app.models.spool_usage_history import SpoolUsageHistory

    query = (
        select(SpoolUsageHistory)
        .order_by(SpoolUsageHistory.created_at.desc(), SpoolUsageHistory.id.desc())
        .limit(limit)
    )
    if printer_id is not None:
        query = query.where(SpoolUsageHistory.printer_id == printer_id)
    result = await db.execute(query)
    return list(result.scalars().all())


async def _return_usage_weight(db: AsyncSession, spool: "Spool | None", rows: list) -> None:
    """Hand the weight recorded by ``rows`` back to the spool and their archives.

    Both delete paths (per-row and clear-all) funnel through here so they behave
    identically. Deleting usage history means "this consumption no longer
    counts", so:

    * ``spool.weight_used`` drops by the rows' total (clamped at 0; the filament
      becomes available again, remaining weight goes up) and the "total consumed"
      baseline is pulled down to stay <= the counter.
    * each linked ``PrintArchive.filament_used_grams`` drops by the weight of the
      rows that referenced it (clamped at 0). Subtracting the *row's* share —
      rather than zeroing the archive — keeps a multi-colour print's total
      correct when only some of its slots are removed, so the Statistics page
      (sum of archive grams) stays equal to inventory (sum of usage rows).

    Caller is responsible for deleting the rows and committing.
    """
    if not rows:
        return
    from backend.app.models.archive import PrintArchive

    total = sum((r.weight_used or 0) for r in rows)
    if spool is not None:
        spool.weight_used = max(0.0, round((spool.weight_used or 0) - total, 1))
        if (spool.weight_used_baseline or 0) > spool.weight_used:
            spool.weight_used_baseline = spool.weight_used
        # Consumption was given back, so the spool may be above its low-stock
        # line again. Re-arm the warning (m117) rather than leave it latched on
        # a figure that is no longer true; the next print re-decides.
        spool.low_stock_notified = False

    per_archive: dict[int, float] = {}
    for r in rows:
        if r.archive_id:
            per_archive[r.archive_id] = per_archive.get(r.archive_id, 0.0) + (r.weight_used or 0)
    for archive_id, used in per_archive.items():
        archive = (await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))).scalar_one_or_none()
        if archive is not None and archive.filament_used_grams is not None:
            archive.filament_used_grams = max(0.0, round(archive.filament_used_grams - used, 1))


@router.delete("/spools/{spool_id}/usage", response_model=SpoolResponse)
async def clear_spool_usage_history(
    spool_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Clear all usage history for a spool, returning the weight to the spool.

    Same accounting as the per-row delete (see :func:`_return_usage_weight`):
    every cleared row's weight goes back to the spool and to its linked archive.
    """
    from backend.app.models.spool_usage_history import SpoolUsageHistory

    spool = (await db.execute(select(Spool).where(Spool.id == spool_id))).scalar_one_or_none()
    if not spool:
        raise HTTPException(404, "Spool not found")

    rows = list(
        (await db.execute(select(SpoolUsageHistory).where(SpoolUsageHistory.spool_id == spool_id))).scalars().all()
    )
    await _return_usage_weight(db, spool, rows)
    for row in rows:
        await db.delete(row)
    await db.commit()

    result = await db.execute(select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id == spool_id))
    await ws_manager.broadcast({"type": "inventory_changed"})
    return result.scalar_one()


@router.delete("/spools/{spool_id}/usage/{usage_id}", response_model=SpoolResponse)
async def delete_spool_usage_record(
    spool_id: int,
    usage_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Delete a single usage-history row and return its weight to the spool.

    See :func:`_return_usage_weight` for the shared accounting (spool weight +
    linked-archive filament both drop by the row's weight, clamped at 0).
    """
    from backend.app.models.spool_usage_history import SpoolUsageHistory

    spool_result = await db.execute(select(Spool).where(Spool.id == spool_id))
    spool = spool_result.scalar_one_or_none()
    if not spool:
        raise HTTPException(404, "Spool not found")

    row_result = await db.execute(
        select(SpoolUsageHistory).where(
            SpoolUsageHistory.id == usage_id,
            SpoolUsageHistory.spool_id == spool_id,
        )
    )
    row = row_result.scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Usage record not found")

    await _return_usage_weight(db, spool, [row])
    await db.delete(row)
    await db.commit()

    result = await db.execute(select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id == spool_id))
    await ws_manager.broadcast({"type": "inventory_changed"})
    return result.scalar_one()


# ── AMS Weight Sync ──────────────────────────────────────────────────────────


@router.post("/sync-ams-weights")
async def sync_weights_from_ams(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Force-sync spool weight_used from live AMS remain% data.

    Overwrites the database weight_used for every assigned spool using the
    current AMS remain% from connected printers.  This is a manual recovery
    tool - it bypasses the normal "only increase" guard.
    """
    from backend.app.services.printer_manager import printer_manager
    from backend.app.services.usage_tracker import record_ams_sync_usage

    result = await db.execute(select(SpoolAssignment).options(selectinload(SpoolAssignment.spool)))
    assignments = list(result.scalars().all())
    logger.info("AMS weight sync: found %d assignments", len(assignments))

    synced = 0
    skipped = 0

    for assignment in assignments:
        spool = assignment.spool
        if not spool:
            logger.debug("AMS weight sync: assignment %d has no spool", assignment.id)
            skipped += 1
            continue

        if spool.weight_locked:
            logger.debug("AMS weight sync: spool %d is weight-locked, skipping", spool.id)
            skipped += 1
            continue

        state = printer_manager.get_status(assignment.printer_id)
        if not state or not state.raw_data:
            logger.info(
                "AMS weight sync: printer %d not connected, skipping spool %d",
                assignment.printer_id,
                spool.id,
            )
            skipped += 1
            continue

        ams_raw = state.raw_data.get("ams", [])
        if isinstance(ams_raw, dict):
            ams_raw = ams_raw.get("ams", [])
        tray = _find_tray_in_ams_data(ams_raw, assignment.ams_id, assignment.tray_id)
        if not tray:
            logger.info(
                "AMS weight sync: no tray data for spool %d (printer %d AMS%d-T%d)",
                spool.id,
                assignment.printer_id,
                assignment.ams_id,
                assignment.tray_id,
            )
            skipped += 1
            continue

        remain_raw = tray.get("remain")
        if remain_raw is None:
            logger.debug("AMS weight sync: no remain value for spool %d", spool.id)
            skipped += 1
            continue

        try:
            remain_val = int(remain_raw)
        except (TypeError, ValueError):
            skipped += 1
            continue

        if remain_val < 0 or remain_val > 100:
            logger.debug("AMS weight sync: invalid remain=%s for spool %d", remain_raw, spool.id)
            skipped += 1
            continue

        # Firmware's own grams when it offers them, the percentage otherwise —
        # BS's precedence, in one place so the three call sites cannot drift.
        new_used = grams_used(tray.get("remain_g"), remain_val, spool.label_weight)
        if new_used is None:
            skipped += 1
            continue
        old_used = spool.weight_used or 0

        if round(old_used, 1) != new_used:
            logger.info(
                "AMS weight sync: spool %d weight_used %s -> %s (remain=%d%%, remain_g=%s)",
                spool.id,
                old_used,
                new_used,
                remain_val,
                tray.get("remain_g"),
            )
            # Shared with the live sync in ``main.on_ams_change``: an increase
            # earns a usage-history row (filament this instance never saw leave
            # the spool), a decrease re-arms the low-stock warning (m117).
            await record_ams_sync_usage(
                db,
                spool,
                printer_id=assignment.printer_id,
                ams_id=assignment.ams_id,
                tray_id=assignment.tray_id,
                new_used=new_used,
            )
            synced += 1
        else:
            skipped += 1

    await db.commit()
    return {"synced": synced, "skipped": skipped}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _find_tray_in_ams_data(ams_data: list, ams_id: int, tray_id: int) -> dict | None:
    """Find a specific tray in the AMS data structure."""
    if not ams_data:
        return None
    for ams_unit in ams_data:
        if int(ams_unit.get("id", -1)) != ams_id:
            continue
        for tray in ams_unit.get("tray", []):
            if int(tray.get("id", -1)) == tray_id:
                return tray
    return None


# ── Filament SKU Settings (reorder forecasting) ───────────────────────────────
#
# Adapted from upstream Bambuddy 37c9d5f2 (#1184). The SKU tuple
# (material, subtype, brand) keys reorder configuration. Forecasting itself
# runs client-side in ForecastPanel.tsx; these endpoints just persist
# operator preferences. Upstream's `RequireAnyPermissionIfAuthEnabled` is
# replaced with our auth-always-on `RequireAnyPermission` per CLAUDE.md.


class FilamentSkuSettingsResponse(BaseModel):
    id: int
    material: str
    subtype: str | None
    brand: str | None
    color_name: str | None
    lead_time_days: int
    safety_margin_value: int
    safety_margin_unit: str
    alerts_snoozed: bool = False

    class Config:
        from_attributes = True


class FilamentSkuSettingsUpsert(BaseModel):
    material: str
    subtype: str | None = None
    brand: str | None = None
    color_name: str | None = None
    lead_time_days: int = Field(0, ge=0)
    safety_margin_value: int = Field(14, ge=0)
    # The forecast math is client-side; this enum is the only server-side
    # guard against a unit no reader understands.
    safety_margin_unit: Literal["days", "g", "kg"] = "days"
    alerts_snoozed: bool = False


@router.get("/sku-settings", response_model=list[FilamentSkuSettingsResponse])
async def list_sku_settings(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAnyPermission(Permission.INVENTORY_READ, Permission.INVENTORY_FORECAST_READ),
):
    """List all filament SKU reorder settings."""
    from backend.app.models.filament_sku_settings import FilamentSkuSettings

    result = await db.execute(
        select(FilamentSkuSettings).order_by(FilamentSkuSettings.material, FilamentSkuSettings.brand)
    )
    return list(result.scalars().all())


@router.post("/sku-settings", response_model=FilamentSkuSettingsResponse)
async def upsert_sku_settings(
    data: FilamentSkuSettingsUpsert,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAnyPermission(Permission.INVENTORY_FORECAST_WRITE, Permission.INVENTORY_UPDATE),
):
    """Create or update reorder settings for a filament SKU (material/subtype/brand)."""
    from backend.app.models.filament_sku_settings import FilamentSkuSettings

    result = await db.execute(
        select(FilamentSkuSettings).where(
            FilamentSkuSettings.material == data.material,
            FilamentSkuSettings.subtype == data.subtype,
            FilamentSkuSettings.brand == data.brand,
            FilamentSkuSettings.color_name == data.color_name,
        )
    )
    row = result.scalar_one_or_none()
    if row:
        row.lead_time_days = data.lead_time_days
        row.safety_margin_value = data.safety_margin_value
        row.safety_margin_unit = data.safety_margin_unit
        row.alerts_snoozed = data.alerts_snoozed
    else:
        row = FilamentSkuSettings(
            material=data.material,
            subtype=data.subtype,
            brand=data.brand,
            color_name=data.color_name,
            lead_time_days=data.lead_time_days,
            safety_margin_value=data.safety_margin_value,
            safety_margin_unit=data.safety_margin_unit,
            alerts_snoozed=data.alerts_snoozed,
        )
        db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


# ── Shopping List ─────────────────────────────────────────────────────────────


class ShoppingListItemResponse(BaseModel):
    id: int
    material: str
    subtype: str | None
    brand: str | None
    color_name: str | None
    quantity_spools: int
    note: str | None
    status: str
    purchased_at: str | None
    added_at: str

    class Config:
        from_attributes = True


class ShoppingListItemCreate(BaseModel):
    material: str
    subtype: str | None = None
    brand: str | None = None
    color_name: str | None = None
    quantity_spools: int = 1
    note: str | None = None


class ShoppingListItemStatusUpdate(BaseModel):
    status: str  # pending | purchased | received


def _shopping_list_item_to_response(item) -> "ShoppingListItemResponse":
    return ShoppingListItemResponse(
        id=item.id,
        material=item.material,
        subtype=item.subtype,
        brand=item.brand,
        color_name=item.color_name,
        quantity_spools=item.quantity_spools,
        note=item.note,
        status=item.status or "pending",
        purchased_at=item.purchased_at.isoformat() if item.purchased_at else None,
        added_at=item.added_at.isoformat() if item.added_at else "",
    )


@router.get("/shopping-list", response_model=list[ShoppingListItemResponse])
async def get_shopping_list(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAnyPermission(Permission.INVENTORY_READ, Permission.INVENTORY_FORECAST_READ),
):
    """Get the filament shopping list."""
    from backend.app.models.shopping_list import ShoppingListItem

    result = await db.execute(select(ShoppingListItem).order_by(ShoppingListItem.added_at.desc()))
    return [_shopping_list_item_to_response(i) for i in result.scalars().all()]


@router.post("/shopping-list", response_model=ShoppingListItemResponse)
async def add_to_shopping_list(
    data: ShoppingListItemCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAnyPermission(Permission.INVENTORY_FORECAST_WRITE, Permission.INVENTORY_UPDATE),
):
    """Add a filament SKU to the shopping list."""
    from backend.app.models.shopping_list import ShoppingListItem

    item = ShoppingListItem(
        material=data.material,
        subtype=data.subtype,
        brand=data.brand,
        color_name=data.color_name,
        quantity_spools=data.quantity_spools,
        note=data.note,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return _shopping_list_item_to_response(item)


@router.patch("/shopping-list/{item_id}/status", response_model=ShoppingListItemResponse)
async def update_shopping_list_status(
    item_id: int,
    data: ShoppingListItemStatusUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAnyPermission(Permission.INVENTORY_FORECAST_WRITE, Permission.INVENTORY_UPDATE),
):
    """Update the purchase status of a shopping list item."""
    from datetime import datetime, timezone

    from backend.app.models.shopping_list import ShoppingListItem

    if data.status not in ("pending", "purchased", "received"):
        raise HTTPException(400, "Invalid status")

    result = await db.execute(select(ShoppingListItem).where(ShoppingListItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Item not found")

    item.status = data.status
    if data.status in ("purchased", "received") and item.purchased_at is None:
        item.purchased_at = datetime.now(timezone.utc)
    elif data.status == "pending":
        item.purchased_at = None

    await db.commit()
    await db.refresh(item)
    return _shopping_list_item_to_response(item)


@router.delete("/shopping-list/{item_id}")
async def remove_from_shopping_list(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAnyPermission(Permission.INVENTORY_FORECAST_WRITE, Permission.INVENTORY_UPDATE),
):
    """Remove a single item from the shopping list."""
    from backend.app.models.shopping_list import ShoppingListItem

    result = await db.execute(select(ShoppingListItem).where(ShoppingListItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Item not found")
    await db.delete(item)
    await db.commit()
    return {"status": "deleted"}


@router.delete("/shopping-list")
async def clear_shopping_list(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAnyPermission(Permission.INVENTORY_FORECAST_WRITE, Permission.INVENTORY_UPDATE),
):
    """Clear all items from the shopping list."""
    from backend.app.models.shopping_list import ShoppingListItem

    result = await db.execute(delete(ShoppingListItem).returning(ShoppingListItem.id))
    deleted = len(result.fetchall())
    await db.commit()
    return {"deleted": deleted}


# ── Server-computed forecast (task 3, 2026-08-29 forecast-server-side) ────────
#
# The four endpoints the Forecast tab renders from. Every number comes from
# `forecast_engine.compute_forecast` (the ONE math owner); sorting, filtering,
# paging and the CSV happen HERE over the finished rows — tens of them — so
# exactly one page's worth leaves the server. The sort semantics port the
# client comparator (ForecastPanel.tsx:361-399) verbatim, including its
# direction-blind 999999 days_left sentinel, plus a stable 4-part-SKU-key
# tiebreak the client never needed (it re-sorted a whole in-memory array; a
# paged walk cannot afford ties resolved by chance).

_FORECAST_SORT_KEYS = frozenset({"material", "spools", "used", "days_left", "stock", "empty_by", "reorder_by"})
_FORECAST_DEFAULT_SORT = "material_asc"  # the client's loadSort fallback: key 'material', dir 'asc'
_FORECAST_CHART_DAY_CHOICES = (7, 30, 180)  # the client's CHART_TIMEFRAMES

# Today's client downloadCsv header strings, en locale (CSV files are data,
# not UI — the same ruling the client CSV lived by; spec §3).
_SHOPPING_LIST_CSV_HEADERS = [
    "Qty",
    "Material",
    "Brand",
    "Subtype",
    "Color",
    "Weight (g)",
    "Lead Time (d)",
    "Expected Restock",
    "Status",
    "Note",
]


def _js_round(value: float) -> int:
    """JS Math.round — halves go UP (toward +∞) where Python's round() banks
    to even. The chart/logistics gram values are ported client pixels; the tie
    behavior stays identical."""
    return math.floor(value + 0.5)


def _forecast_has_alert(row: forecast_engine.SkuForecastRow) -> bool:
    """The client's badge predicate: an un-snoozed stock-break or reorder."""
    return (row.stock_break_alert or row.reorder_alert) and not row.alerts_snoozed


def _forecast_sort_rows(
    rows: list[forecast_engine.SkuForecastRow], sort_by: str | None
) -> list[forecast_engine.SkuForecastRow]:
    """The client comparator, server-side, over finished rows.

    Dateless rows sink to the end whatever the direction (the client flips the
    Infinity sentinel WITH the direction); ``days_left`` instead keeps the
    client's direction-blind 999999 — so rate-less rows LEAD a descending
    days_left sort, a quirk ported deliberately rather than "fixed".
    """
    sort_by = sort_by or _FORECAST_DEFAULT_SORT
    key_name, sep, direction = sort_by.rpartition("_")
    if not sep or direction not in ("asc", "desc") or key_name not in _FORECAST_SORT_KEYS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported sort_by — expected <key>_asc|_desc with key one of: "
                + ", ".join(sorted(_FORECAST_SORT_KEYS))
            ),
        )
    descending = direction == "desc"
    dateless = -math.inf if descending else math.inf

    def primary(row: forecast_engine.SkuForecastRow) -> float | str:
        if key_name == "material":
            # The client's composite: [material, subtype ?? '', brand ?? ''].join(' ').toLowerCase()
            return " ".join((row.material or "", row.subtype or "", row.brand or "")).lower()
        if key_name == "spools":
            return row.total_spools
        if key_name == "used":
            return row.total_used_g
        if key_name == "days_left":
            return row.days_remaining if row.days_remaining is not None else 999999
        if key_name == "stock":
            return row.total_remaining_g
        anchor = row.projected_empty_date if key_name == "empty_by" else row.reorder_trigger_date
        return anchor.toordinal() if anchor is not None else dateless

    # Two stable passes: collapsed SKU key first, the primary second. Python's
    # sort keeps equal elements in place even with reverse=True, so equal
    # primaries stay in ascending-SKU order in BOTH directions — which is what
    # makes a page walk over ties repeat-free and skip-free.
    ordered = sorted(rows, key=lambda r: forecast_engine.sku_key(r.material, r.subtype, r.brand, r.color_name))
    ordered.sort(key=primary, reverse=descending)
    return ordered


@router.get("/forecast", response_model=ForecastListPage)
async def get_inventory_forecast(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    all: bool = Query(False, description="Skip pagination and return every matching row"),
    sort_by: str | None = Query(
        None, description="<key>_asc|_desc over the client sort-key set; omitted means material_asc"
    ),
    material: str | None = Query(None, description="Exact match"),
    brand: str | None = Query(None, description="Exact match"),
    alerts_only: bool = Query(False, description="Only rows with an un-snoozed stock-break or reorder alert"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAnyPermission(Permission.INVENTORY_READ, Permission.INVENTORY_FORECAST_READ),
) -> ForecastListPage:
    """One server-sorted, server-filtered page of finished forecast rows.

    ``alert_count`` counts un-snoozed alert rows across the WHOLE farm — the
    client's badge read the unfiltered set, so the filters must not move it;
    ``meta.total`` counts the filtered set.
    """
    rows = await forecast_engine.compute_forecast(db)
    alert_count = sum(1 for r in rows if _forecast_has_alert(r))

    if material is not None:
        rows = [r for r in rows if r.material == material]
    if brand is not None:
        rows = [r for r in rows if r.brand == brand]
    if alerts_only:
        rows = [r for r in rows if _forecast_has_alert(r)]

    ordered = _forecast_sort_rows(rows, sort_by)
    total = len(ordered)
    page_rows = ordered if all else ordered[(page - 1) * per_page : (page - 1) * per_page + per_page]

    return ForecastListPage(
        items=[SkuForecastRowResponse.model_validate(r) for r in page_rows],
        meta=PaginationMeta(
            total=total,
            current_page=1 if all else page,
            per_page=(total or 1) if all else per_page,
            last_page=1 if all else max(1, math.ceil(total / per_page)),
        ),
        alert_count=alert_count,
        global_lead_time_days=await forecast_engine._global_lead_time_days(db),
    )


@router.get("/forecast/chart", response_model=ForecastChartResponse)
async def get_inventory_forecast_chart(
    days: int = Query(30, description="7, 30 or 180 — the client's chart timeframes"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAnyPermission(Permission.INVENTORY_READ, Permission.INVENTORY_FORECAST_READ),
) -> ForecastChartResponse:
    """The top-5 SKUs by burned grams: day-bucketed usage + depletion projection.

    The usage series is a NEW capability (spec §2.2 as corrected after the T2
    review — the shipped client chart drew the projection only): the record of
    what was burned, reset spools included. The projection ports
    ``buildProjectionSeries`` — Math.round for display, clamp at zero, stop
    after pushing the first zero.
    """
    if days not in _FORECAST_CHART_DAY_CHOICES:
        raise HTTPException(status_code=400, detail="days must be one of 7, 30, 180")

    now = datetime.now(timezone.utc)
    today = now.date()
    rows = await forecast_engine.compute_forecast(db, now=now)

    # The client drops rate-less rows BEFORE ranking (`dailyRateG !== null`),
    # then takes the 5 biggest consumers. The stable sort keeps the collapsed-
    # SKU order compute_forecast returns as the deterministic tie order.
    candidates = [r for r in rows if r.rate_g_day is not None]
    candidates.sort(key=lambda r: r.total_used_g, reverse=True)
    top = candidates[:5]

    sku_keys = [(r.material, r.subtype, r.brand, r.color_name) for r in top]
    usage = await forecast_engine.usage_day_series(db, sku_keys=sku_keys, days=days, now=now) if sku_keys else {}

    series: list[ForecastChartSeries] = []
    for row in top:
        rate = row.rate_g_day
        projection: list[tuple[date, int]] = []
        for offset in range(days + 1):
            raw = max(0.0, row.total_remaining_g - rate * offset)
            projection.append((today + timedelta(days=offset), _js_round(raw)))
            if raw == 0:
                break
        series.append(
            ForecastChartSeries(
                sku=ForecastChartSku(
                    material=row.material, subtype=row.subtype, brand=row.brand, color_name=row.color_name
                ),
                rgba=row.rgba,
                rop_g=row.reorder_point_g,
                usage=usage.get((row.material, row.subtype, row.brand, row.color_name), []),
                projection=projection,
            )
        )
    return ForecastChartResponse(series=series)


@router.get("/forecast/logistics", response_model=list[ForecastLogisticsRow])
async def get_inventory_forecast_logistics(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAnyPermission(Permission.INVENTORY_READ, Permission.INVENTORY_FORECAST_READ),
) -> list[ForecastLogisticsRow]:
    """``CartLogisticsRow``'s computation for every shopping-list item, in the
    shopping-list GET's order (added_at desc — the set the panel renders).

    The series keeps the client's vertical-step trick: the arrival date appears
    twice (just-before, just-after the parcel lands). An item whose SKU has no
    forecast row or no positive rate gets ``series: null`` — the client's
    "no usage data" placeholder case, never an error.
    """
    from backend.app.models.shopping_list import ShoppingListItem

    now = datetime.now(timezone.utc)
    today = now.date()
    rows = await forecast_engine.compute_forecast(db, now=now)
    by_key = {forecast_engine.sku_key(r.material, r.subtype, r.brand, r.color_name): r for r in rows}

    items = (await db.execute(select(ShoppingListItem).order_by(ShoppingListItem.added_at.desc()))).scalars().all()

    out: list[ForecastLogisticsRow] = []
    for item in items:
        row = by_key.get(forecast_engine.sku_key(item.material, item.subtype, item.brand, item.color_name))
        if row is None or row.rate_g_day is None or row.rate_g_day <= 0:
            out.append(
                ForecastLogisticsRow(
                    item_id=item.id,
                    series=None,
                    arrival_day=None,
                    rop_g=None,
                    safety_stock_g=None,
                    stock_break_before_arrival=False,
                )
            )
            continue

        rate = row.rate_g_day
        lead = row.eff_lead_time_days
        avg_spool_g = row.total_label_g / row.total_spools if row.total_spools > 0 else 1000.0
        arrival_g = item.quantity_spools * avg_spool_g
        stock_at_arrival = max(0.0, row.total_remaining_g - rate * lead)
        peak_g = stock_at_arrival + arrival_g
        clamped_max = min(lead + math.ceil(peak_g / rate) + 5, 365)

        series: list[tuple[date, int]] = []
        for offset in range(clamped_max + 1):
            day = today + timedelta(days=offset)
            if offset == lead:
                series.append((day, _js_round(stock_at_arrival)))
                series.append((day, _js_round(peak_g)))
            elif offset < lead:
                series.append((day, _js_round(max(0.0, row.total_remaining_g - rate * offset))))
            else:
                series.append((day, _js_round(max(0.0, peak_g - rate * (offset - lead)))))

        out.append(
            ForecastLogisticsRow(
                item_id=item.id,
                series=series,
                arrival_day=lead,
                rop_g=row.reorder_point_g,
                safety_stock_g=row.safety_stock_g,
                stock_break_before_arrival=math.floor(row.total_remaining_g / rate) < lead,
            )
        )
    return out


@router.get("/shopping-list/export.csv")
async def export_shopping_list_csv(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAnyPermission(Permission.INVENTORY_READ, Permission.INVENTORY_FORECAST_READ),
) -> Response:
    """The shopping list as CSV — today's client ``downloadCsv``, server-made.

    Columns and the everything-quoted style are the client's; the restock date
    is ISO instead of the viewer-locale format (the server has no viewer
    locale — a named deviation, the columns otherwise identical).
    """
    from backend.app.models.shopping_list import ShoppingListItem

    now = datetime.now(timezone.utc)
    today = now.date()
    rows = await forecast_engine.compute_forecast(db, now=now)
    by_key = {forecast_engine.sku_key(r.material, r.subtype, r.brand, r.color_name): r for r in rows}
    global_lead = await forecast_engine._global_lead_time_days(db)

    items = (await db.execute(select(ShoppingListItem).order_by(ShoppingListItem.added_at.desc()))).scalars().all()

    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL)
    writer.writerow(_SHOPPING_LIST_CSV_HEADERS)
    for item in items:
        row = by_key.get(forecast_engine.sku_key(item.material, item.subtype, item.brand, item.color_name))
        avg_spool_g = row.total_label_g / row.total_spools if row is not None and row.total_spools > 0 else 1000.0
        lead = row.eff_lead_time_days if row is not None else global_lead
        restock = (today + timedelta(days=lead)).isoformat() if lead > 0 else ""
        writer.writerow(
            [
                item.quantity_spools,
                item.material,
                item.brand or "",
                item.subtype or "",
                item.color_name or "",
                _js_round(item.quantity_spools * avg_spool_g),
                lead or "",
                restock,
                item.status or "pending",
                item.note or "",
            ]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="shopping-list.csv"'},
    )


class CreateSpoolFromSlotRequest(BaseModel):
    printer_id: int
    ams_id: int
    tray_id: int


@router.post("/spools/from-slot", response_model=SpoolResponse)
async def create_spool_from_slot(
    req: CreateSpoolFromSlotRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.INVENTORY_UPDATE),
):
    """Explicit user action: create an inventory spool from an AMS slot's current tray data.

    Used by the "+ Add to inventory" affordance when auto_add_unknown_rfid is disabled —
    the user looked at the slot and chose to register it. Also assigns the new spool
    to the slot in the same call.
    """
    from backend.app.services.printer_manager import printer_manager
    from backend.app.services.spool_tag_matcher import auto_assign_spool, create_spool_from_tray

    state = printer_manager.get_status(req.printer_id)
    if not state or not state.raw_data:
        raise HTTPException(status_code=404, detail="Printer not connected or no state available")

    ams_data = state.raw_data.get("ams")
    ams_units: list[dict] = []
    if isinstance(ams_data, list):
        ams_units = ams_data
    elif isinstance(ams_data, dict):
        if "ams" in ams_data and isinstance(ams_data["ams"], list):
            ams_units = ams_data["ams"]
        elif "tray" in ams_data:
            ams_units = [{"id": 0, "tray": ams_data.get("tray", [])}]

    tray: dict | None = None
    for unit in ams_units:
        if not isinstance(unit, dict):
            continue
        if int(unit.get("id", -1)) != req.ams_id:
            continue
        for t in unit.get("tray", []):
            if isinstance(t, dict) and int(t.get("id", -1)) == req.tray_id:
                tray = t
                break
        if tray:
            break

    if not tray or not tray.get("tray_type"):
        raise HTTPException(status_code=400, detail="Slot is empty or has no readable tray data")

    # Guard against ghost-spool creation: a slot without any RFID tag has no
    # stable identity, so creating an inventory row would just duplicate on
    # every confirm and never re-link to the physical spool.
    from backend.app.services.spool_tag_matcher import is_valid_tag

    if not is_valid_tag(tray.get("tag_uid", ""), tray.get("tray_uuid", "")):
        raise HTTPException(status_code=400, detail="Slot has no RFID tag")

    spool = await create_spool_from_tray(db, tray)
    await auto_assign_spool(
        req.printer_id,
        req.ams_id,
        req.tray_id,
        spool,
        printer_manager,
        db,
        tray_info_idx=tray.get("tray_info_idx", ""),
    )
    await db.commit()
    await ws_manager.broadcast({"type": "inventory_changed"})
    await ws_manager.broadcast(
        {
            "type": "spool_auto_assigned",
            "printer_id": req.printer_id,
            "ams_id": req.ams_id,
            "tray_id": req.tray_id,
            "spool_id": spool.id,
        }
    )
    result = await db.execute(select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id == spool.id))
    return result.scalar_one()
