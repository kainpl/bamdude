"""What the Slice dialog's two farm filters ask: which printers are online, and
which filaments are loaded on them (owner's decision 7, upstream 7f00816f7 as
the behaviour reference).

Nothing here builds a loaded-filament list of its own. A printer's sources come
from its feed snapshot (``printer_manager.get_feed_snapshot``), so:

* a slot BamDude advertised under another profile (AMS backup compatibility)
  reports the spool it really holds — ``FeedSource.material`` / ``variant`` are
  the spool's, the advertisement is ``declared_*`` and is not read here;
* an external holder counts, an empty slot does not;
* the same answer in built-in inventory and in Spoolman mode — the snapshot is
  what the slot was told for its assigned spool in either, and no inventory is
  asked per request.

A filament's identity is its FAMILY (``variant`` — the filament id the slot
carries). The presets that ARE that family are named per source tier of the
preset listing, by the id that tier lists them under — never by display name.
A source that states only a material — nothing recognisable on the slot, a
Generic family, or a borrowed profile whose type is not the slot's (a spool
without a family keeps ITS type; only the profile is borrowed) — is matched by
material type instead (``by_material``).

Upstream read ``slot_preset_mappings``; that table was removed in m150 and is
not coming back — the slot's identity is the resolver's.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.printer import Printer
from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset
from backend.app.schemas.slicer_presets import (
    FamilyPresetRefs,
    FarmPrinter,
    LoadedFilament,
    LoadedFilamentsResponse,
    OnlinePrinter,
)
from backend.app.utils import filament_catalog as catalog
from backend.app.utils.filament_types import filament_types_compatible
from backend.app.utils.model_compatibility import compatible_models
from backend.app.utils.printer_models import normalize_model_name


def _base_setting_id(setting_id: str) -> str:
    return setting_id.split("_")[0]


async def online_printers(db: AsyncSession, printer_manager, allowed: frozenset[int] | None) -> list[Printer]:
    """Printers available NOW: active, not archived, connected — within ``allowed``
    (an API key's printer list; ``None`` is every printer)."""
    rows = (
        (
            await db.execute(
                select(Printer).where(Printer.is_active.is_(True), Printer.archived.is_(False)).order_by(Printer.id)
            )
        )
        .scalars()
        .all()
    )
    return [
        printer
        for printer in rows
        if (allowed is None or printer.id in allowed) and printer_manager.is_connected(printer.id)
    ]


async def farm_printers(db: AsyncSession, printer_manager, allowed: frozenset[int] | None) -> list[FarmPrinter]:
    """Every printer of the farm — not archived, within ``allowed`` (an API key's
    printer list; ``None`` is every printer) — whatever its state: online,
    paused, offline or in maintenance (spec slicing-setup D4). The rows are
    selected before any live state is read; the model is the live one when the
    printer is connected, else the row's, and the nozzles are known only from a
    live connection — never an assumed size."""
    rows = (await db.execute(select(Printer).where(Printer.archived.is_(False)).order_by(Printer.id))).scalars().all()
    out: list[FarmPrinter] = []
    for printer in rows:
        if allowed is not None and printer.id not in allowed:
            continue
        snapshot = printer_manager.get_feed_snapshot(printer.id) if printer_manager.is_connected(printer.id) else None
        model = (snapshot.model if snapshot is not None else None) or normalize_model_name(printer.model)
        nozzles = (
            sorted({d for sizes in snapshot.nozzle_diameters.values() for d in sizes}) if snapshot is not None else []
        )
        out.append(
            FarmPrinter(
                printer_id=printer.id,
                model=model,
                accepts=sorted(compatible_models(model)) if model else [],
                nozzle_diameters=nozzles,
            )
        )
    return out


async def _family_refs(
    db: AsyncSession, family_ids: set[str], user_families: dict[str, UserFilamentFamily], user_id: int | None
) -> dict[str, FamilyPresetRefs]:
    """Every preset that IS one of these families, by the id its tier lists it under."""
    if not family_ids:
        return {}
    owner = (
        or_(UserFilamentPreset.owner_user_id.is_(None), UserFilamentPreset.owner_user_id == user_id)
        if user_id is not None
        else UserFilamentPreset.owner_user_id.is_(None)
    )
    mirrors = (
        (
            await db.execute(
                select(UserFilamentPreset).where(UserFilamentPreset.family_filament_id.in_(family_ids), owner)
            )
        )
        .scalars()
        .all()
    )
    out: dict[str, FamilyPresetRefs] = {}
    for fid in sorted(family_ids):
        system = catalog.get_family(fid)
        user = user_families.get(fid)
        if system is None and user is None:
            continue
        standard: set[str] = set()
        cloud: set[str] = set()
        for preset in catalog.presets_for_family(fid):
            standard.add(preset.name)
            if preset.ecosystem == "bambu" and preset.setting_id:
                cloud.update((preset.setting_id, _base_setting_id(preset.setting_id)))
        orca_cloud: set[str] = set()
        local: set[str] = set()
        for row in mirrors:
            if row.family_filament_id != fid:
                continue
            if row.source == "cloud_bambu" and row.cloud_id:
                cloud.add(row.cloud_id)
            if row.source == "cloud_orca" and row.cloud_id:
                orca_cloud.add(row.cloud_id)
            if row.local_preset_id is not None:
                local.add(str(row.local_preset_id))
            if row.pushed_cloud_id:
                cloud.add(row.pushed_cloud_id)
            if row.orca_pushed_profile_id:
                orca_cloud.add(row.orca_pushed_profile_id)
        out[fid] = FamilyPresetRefs(
            name=(system.alias if system else user.alias),
            cloud=sorted(cloud),
            orca_cloud=sorted(orca_cloud),
            local=sorted(local),
            standard=sorted(standard),
        )
    return out


async def loaded_filaments(
    db: AsyncSession, printer_manager, *, allowed: frozenset[int] | None, user_id: int | None
) -> LoadedFilamentsResponse:
    printers = await online_printers(db, printer_manager, allowed)
    snapshots = {printer.id: printer_manager.get_feed_snapshot(printer.id) for printer in printers}
    named = {
        (source.variant or "").strip()
        for snapshot in snapshots.values()
        for source in snapshot.sources
        if (source.variant or "").strip()
    }
    user_families = (
        {
            row.filament_id: row
            for row in (await db.execute(select(UserFilamentFamily).where(UserFilamentFamily.filament_id.in_(named))))
            .scalars()
            .all()
        }
        if named
        else {}
    )
    families = await _family_refs(db, named, user_families, user_id)
    generic = catalog.generic_family_ids()

    out: list[OnlinePrinter] = []
    for printer in printers:
        snapshot = snapshots[printer.id]
        model = snapshot.model or normalize_model_name(printer.model)
        filaments: list[LoadedFilament] = []
        for source in snapshot.sources:
            material = (source.material or "").strip()
            fid = (source.variant or "").strip()
            family_id = fid if fid in families else None
            family = (catalog.get_family(fid) or user_families.get(fid)) if family_id else None
            family_type = family.filament_type if family else None
            by_material = (
                family_id is None
                or family_id in generic
                or not filament_types_compatible((family_type or "").strip(), material)
            )
            entry = LoadedFilament(kind=source.kind, material=material, family_id=family_id, by_material=by_material)
            if entry not in filaments:
                filaments.append(entry)
        out.append(
            OnlinePrinter(
                printer_id=printer.id,
                model=model,
                accepts=sorted(compatible_models(model)) if model else [],
                nozzle_diameters=sorted({d for sizes in snapshot.nozzle_diameters.values() for d in sizes}),
                filaments=filaments,
            )
        )
    used = {f.family_id for p in out for f in p.filaments if f.family_id}
    return LoadedFilamentsResponse(
        printers=out,
        farm_printers=await farm_printers(db, printer_manager, allowed),
        families={fid: refs for fid, refs in families.items() if fid in used},
    )
