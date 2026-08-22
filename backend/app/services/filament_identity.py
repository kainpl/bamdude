"""THE identity authority (spec A §4): anything -> family. Pure memory (system
catalog) + DB (mirrors). NO network calls in this module, ever — a mirror
miss means "stale, the sync loop will catch it", never a live cloud call on
the assignment path. Slot assignment must work offline.

Wire identity is the bare filament_id string, exactly as BambuStudio sends it
(tray_info_idx). GF* families are shared across ecosystems (Orca mirrors BBL)
so their canonical ecosystem is "bambu"; P* families are ecosystem-specific
and carry it from their user_filament_families row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset
from backend.app.utils import filament_catalog as catalog
from backend.app.utils.filament_ids import setting_id_to_filament_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FamilyRef:
    filament_id: str
    ecosystem: str | None


@dataclass(frozen=True)
class ResolvedFilament:
    family: FamilyRef | None
    display_name: str
    setting_id: str | None
    nozzle_temp_min: int | None
    nozzle_temp_max: int | None
    vendor: str | None
    filament_type: str | None
    origin: str  # system | cloud_bambu | cloud_orca | local | legacy | unknown


_UNKNOWN = ResolvedFilament(None, "", None, None, None, None, None, "unknown")


def _from_system_family(fam: catalog.CatalogFamily, preset: catalog.CatalogPreset | None = None) -> ResolvedFilament:
    return ResolvedFilament(
        family=FamilyRef(fam.filament_id, "bambu"),
        display_name=fam.alias,
        setting_id=preset.setting_id if preset else None,
        nozzle_temp_min=preset.nozzle_temp_min if preset else None,
        nozzle_temp_max=preset.nozzle_temp_max if preset else None,
        vendor=fam.vendor,
        filament_type=fam.filament_type,
        origin="system",
    )


async def _user_family(db: AsyncSession, filament_id: str) -> UserFilamentFamily | None:
    return (
        (await db.execute(select(UserFilamentFamily).where(UserFilamentFamily.filament_id == filament_id)))
        .scalars()
        .first()
    )


def _from_user_family(
    fam: UserFilamentFamily,
    *,
    setting_id: str | None = None,
    temps: tuple[int | None, int | None] = (None, None),
    origin: str | None = None,
    display_name: str | None = None,
) -> ResolvedFilament:
    return ResolvedFilament(
        family=FamilyRef(fam.filament_id, fam.ecosystem),
        display_name=display_name or fam.alias,
        setting_id=setting_id,
        nozzle_temp_min=temps[0],
        nozzle_temp_max=temps[1],
        vendor=fam.vendor,
        filament_type=fam.filament_type,
        origin=origin or fam.origin,
    )


async def resolve_tray(db: AsyncSession, tray_info_idx: str | None) -> ResolvedFilament:
    """Printer-reported tray identity -> family. Catalog first, user families second."""
    fid = (tray_info_idx or "").strip()
    if not fid:
        return _UNKNOWN
    fam = catalog.get_family(fid)
    if fam:
        return _from_system_family(fam)
    user_fam = await _user_family(db, fid)
    if user_fam:
        return _from_user_family(user_fam)
    return _UNKNOWN


async def _resolve_mirror_row(db: AsyncSession, row: UserFilamentPreset) -> ResolvedFilament:
    if not row.family_filament_id:
        return _UNKNOWN
    fam = catalog.get_family(row.family_filament_id)
    if fam:
        base = _from_system_family(fam)
        return ResolvedFilament(
            family=base.family,
            display_name=row.name,
            setting_id=row.cloud_id,
            nozzle_temp_min=row.nozzle_temp_min,
            nozzle_temp_max=row.nozzle_temp_max,
            vendor=row.vendor or base.vendor,
            filament_type=row.filament_type or base.filament_type,
            origin=row.source,
        )
    user_fam = await _user_family(db, row.family_filament_id)
    if user_fam:
        return _from_user_family(
            user_fam,
            setting_id=row.cloud_id,
            temps=(row.nozzle_temp_min, row.nozzle_temp_max),
            origin=row.source,
            display_name=row.name,
        )
    return _UNKNOWN


async def resolve_raw(db: AsyncSession, raw: str | None, *, owner_user_id: int | None = None) -> ResolvedFilament:
    """Decode the legacy union column: GF*/GFS* ids, PFUS*/uuid cloud ids,
    LocalPreset int PKs, bare material names — the ONE place the old formats
    are still understood. (owner_user_id narrows mirror hits when given.)"""
    value = (raw or "").strip()
    if not value:
        return _UNKNOWN
    base = value.split("_")[0] if value.startswith("GF") and "_" in value else value

    # 1) System catalog: filament_id or setting_id in either form.
    fam = catalog.get_family(base)
    if fam:
        return _from_system_family(fam)
    preset = catalog.preset_for_setting_id(value) or catalog.preset_for_setting_id(base)
    if preset:
        preset_fam = catalog.get_family(preset.filament_id)
        if preset_fam:
            return _from_system_family(preset_fam, preset)

    # 2) Mirrors: cloud id (PFUS… / Orca uuid) or local preset PK.
    query = select(UserFilamentPreset).where(UserFilamentPreset.cloud_id == base)
    if owner_user_id is not None:
        query = query.where(UserFilamentPreset.owner_user_id == owner_user_id)
    row = (await db.execute(query)).scalars().first()
    if row is None and value.isdigit():
        row = (
            (await db.execute(select(UserFilamentPreset).where(UserFilamentPreset.local_preset_id == int(value))))
            .scalars()
            .first()
        )
    if row is not None:
        resolved = await _resolve_mirror_row(db, row)
        if resolved.family:
            return resolved

    # 3) User family referenced directly by its P-hash.
    user_fam = await _user_family(db, base)
    if user_fam:
        return _from_user_family(user_fam)

    # 4) Legacy heuristics — last resort, warning-logged.
    stripped = setting_id_to_filament_id(base)
    if stripped != base:
        fam = catalog.get_family(stripped)
        if fam:
            logger.warning("filament_identity: legacy GFS->GF fallback used for %r", raw)
            return _from_system_family(fam)
    generic = catalog.generic_family_for_material(value)
    if generic:
        logger.warning("filament_identity: material-name fallback %r -> %s", raw, generic.filament_id)
        return replace(_from_system_family(generic), origin="legacy")
    return _UNKNOWN


async def resolve_spool(db: AsyncSession, spool) -> ResolvedFilament:
    """Family link -> RFID -> legacy string (spec A §5.1 precedence)."""
    family_id = getattr(spool, "filament_family_id", None)
    if family_id:
        resolved = await resolve_tray(db, family_id)
        if resolved.family:
            return resolved
    rfid = getattr(spool, "bambu_filament_id", None)
    if rfid:
        resolved = await resolve_tray(db, rfid)
        if resolved.family:
            return resolved
    return await resolve_raw(db, getattr(spool, "slicer_filament", None))
