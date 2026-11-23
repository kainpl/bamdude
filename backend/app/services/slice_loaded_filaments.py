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

from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core import auth_lifetime
from backend.app.models.local_preset import LocalPreset
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset
from backend.app.schemas.slicer_presets import (
    FamilyPresetRefs,
    FarmPrinter,
    LoadedFilament,
    LoadedFilamentsResponse,
    OnlinePrinter,
)
from backend.app.services import cloud_credentials
from backend.app.services.bambu_cloud_credentials import get_stored_token
from backend.app.utils import filament_catalog as catalog
from backend.app.utils.filament_types import filament_types_compatible, support_slot_type
from backend.app.utils.model_compatibility import compatible_models
from backend.app.utils.printer_models import normalize_model_name


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


Provider = Literal["bambu", "orca"]
_PROVIDER_RIGHT: dict[str, str] = {"bambu": "cloud:auth", "orca": "orca_cloud:auth"}


@dataclass(frozen=True)
class ProviderView:
    """One provider's consistent credential state for this read: the snapshot
    whose generation and authority the final re-check compares against."""

    snapshot: cloud_credentials.CredentialSnapshot


@dataclass(frozen=True)
class FamilyRefViewer:
    """Who is reading the family refs (spec slicing-setup r5 §8.4): the
    principal's id for mirror ownership, and per provider the view a cloud
    bucket may be projected for — ``None`` means that bucket is empty."""

    user_id: int | None
    bambu: ProviderView | None
    orca: ProviderView | None


class _Unstable(Exception):
    """The active identity or the provider link moved while it was read."""


async def _gate_once(db: AsyncSession, principal: User, provider: Provider) -> ProviderView | None:
    right = _PROVIDER_RIGHT[provider]
    try:
        snap = await cloud_credentials.snapshot(db, principal, provider)
    except HTTPException as exc:
        if exc.status_code == 503:
            raise
        if exc.status_code == 409:
            raise _Unstable from exc
        return None
    if snap.authority is not None:
        try:
            row = (await auth_lifetime.validate_captured(db, snap.authority, required_permissions=(right,))).user
        except HTTPException as exc:
            if exc.status_code == 503:
                raise
            return None
        except Exception:
            return None
    else:
        row = await db.scalar(
            select(User)
            .where(User.id == principal.id)
            .options(selectinload(User.groups))
            .execution_options(populate_existing=True)
        )
    if row is None or not row.is_active:
        return None
    if row.account_identity != snap.account_identity or getattr(row, f"{provider}_cloud_generation") != snap.generation:
        raise _Unstable
    if not row.has_permission(right):
        return None
    if provider == "bambu":
        token, _email, _region = await get_stored_token(db, row)
    else:
        token = row.orca_cloud_token
    return ProviderView(snap) if token else None


async def provider_gate(db: AsyncSession, principal: User, provider: Provider) -> ProviderView | None:
    """One consistent local state for one provider, or ``None``: the right, the
    principal's OWN stored credentials and a stable identity and generation.
    No OAuth loader, never a configured-global credential. A moving identity is
    tried once more; a second failure in a row is ``None``. Only a 503
    propagates — the rest of the answer is always served."""
    for _attempt in range(2):
        try:
            return await _gate_once(db, principal, provider)
        except _Unstable:
            continue
    return None


async def family_ref_viewer(db: AsyncSession, user: User | None, key_owner: User | None) -> FamilyRefViewer:
    """The principal is the signed-in user, else the owner behind a cloud-allowed
    API key. Without one (an ownerless or cloud-denied key) no cloud bucket is
    projected and no snapshot is taken."""
    principal = user or key_owner
    if principal is None:
        return FamilyRefViewer(None, None, None)
    return FamilyRefViewer(
        principal.id,
        await provider_gate(db, principal, "bambu"),
        await provider_gate(db, principal, "orca"),
    )


async def recheck_provider(
    db: AsyncSession, view: ProviderView, principal: User, provider: Provider
) -> Literal["same", "generation_changed", "lost"]:
    """A fresh read after the mirror rows: the snapshot still current, or the
    access re-confirmed under a new link (private refs drop), or lost (the whole
    bucket goes)."""
    if await cloud_credentials.current(db, view.snapshot):
        return "same"
    return "lost" if await provider_gate(db, principal, provider) is None else "generation_changed"


async def _read_mirrors(db: AsyncSession, family_ids: set[str]) -> list[UserFilamentPreset]:
    """Every mirror row of these families, whoever owns it: which of its fields
    the caller may see is decided by the projection, not by this read."""
    return list(
        (await db.execute(select(UserFilamentPreset).where(UserFilamentPreset.family_filament_id.in_(family_ids))))
        .scalars()
        .all()
    )


async def _family_refs(
    db: AsyncSession,
    family_ids: set[str],
    user_families: dict[str, UserFilamentFamily],
    viewer: FamilyRefViewer,
    principal: User | None,
) -> dict[str, FamilyPresetRefs]:
    """The refs a caller may see of each recognised family (spec §8.4).

    Standard names and local preset ids are public. A Bambu / Orca Cloud bucket
    exists only for a provider the viewer passed the gate for; inside it, a
    mirror's cloud id only for the caller's own mirror, a push anchor only for
    the caller's own account identity. The provider is re-checked after the
    mirror rows were read: ``lost`` empties the whole bucket (system ids and
    aliases too), ``generation_changed`` drops only the private ids. A custom
    family's name comes last, from the refs that survived.
    """
    if not family_ids:
        return {}
    mirrors = await _read_mirrors(db, family_ids)
    status: dict[str, str] = {}
    for provider, view in (("bambu", viewer.bambu), ("orca", viewer.orca)):
        if view is None or principal is None:
            status[provider] = "lost"
        else:
            status[provider] = await recheck_provider(db, view, principal, provider)
    private = {provider: state == "same" for provider, state in status.items()}
    bucket = {provider: state != "lost" for provider, state in status.items()}
    identity = {
        "bambu": viewer.bambu.snapshot.account_identity if viewer.bambu else None,
        "orca": viewer.orca.snapshot.account_identity if viewer.orca else None,
    }

    local_names: dict[int, str] = {}
    local_ids = {row.local_preset_id for row in mirrors if row.local_preset_id is not None}
    if local_ids:
        local_names = dict(
            (await db.execute(select(LocalPreset.id, LocalPreset.name).where(LocalPreset.id.in_(local_ids)))).all()
        )
    out: dict[str, FamilyPresetRefs] = {}
    for fid in sorted(family_ids):
        system = catalog.get_family(fid)
        if system is None and fid not in user_families:
            continue
        standard: set[str] = set()
        cloud: set[str] = set()
        for preset in catalog.presets_for_family(fid):
            standard.add(preset.name)
            # Exact ids only: a base is no id of its own (spec §8.3) — the
            # unambiguous ones travel as ``cloud_aliases``.
            if bucket["bambu"] and preset.ecosystem == "bambu" and preset.setting_id:
                cloud.add(preset.setting_id)
        orca_cloud: set[str] = set()
        local: set[int] = set()
        own_names: list[tuple[int, str]] = []
        for row in sorted((r for r in mirrors if r.family_filament_id == fid), key=lambda r: r.id):
            if row.local_preset_id is not None and row.local_preset_id in local_names:
                local.add(row.local_preset_id)
            mine = viewer.user_id is not None and row.owner_user_id == viewer.user_id
            seen = False
            if private["bambu"]:
                if row.source == "cloud_bambu" and row.cloud_id and mine:
                    cloud.add(row.cloud_id)
                    seen = True
                if row.pushed_cloud_id and row.bambu_pushed_owner_identity == identity["bambu"]:
                    cloud.add(row.pushed_cloud_id)
                    seen = True
            if private["orca"]:
                if row.source == "cloud_orca" and row.cloud_id and mine:
                    orca_cloud.add(row.cloud_id)
                    seen = True
                if row.orca_pushed_profile_id and row.orca_pushed_owner_identity == identity["orca"]:
                    orca_cloud.add(row.orca_pushed_profile_id)
                    seen = True
            if seen:
                own_names.append((row.id, row.name))
        if system is not None:
            name: str | None = system.alias
        elif own_names:
            name = min(own_names)[1]
        elif local:
            name = local_names[min(local)]
        else:
            # A custom family's stored alias is global and may come from another
            # user's private mirror: with no visible ref there is no name.
            name = None
        out[fid] = FamilyPresetRefs(
            name=name,
            cloud=sorted(cloud),
            orca_cloud=sorted(orca_cloud),
            local=[str(i) for i in sorted(local)],
            standard=sorted(standard),
            cloud_aliases=sorted(catalog.alias_bases_for_family(fid)) if bucket["bambu"] else [],
        )
    return out


async def loaded_filaments(
    db: AsyncSession,
    printer_manager,
    *,
    allowed: frozenset[int] | None,
    user: User | None,
    key_owner: User | None = None,
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
    # Recognised first, independent of what the caller may see (spec §8.4):
    # hiding refs never changes a source's family or its by_material basis.
    recognised = {fid for fid in named if catalog.get_family(fid) is not None or fid in user_families}
    viewer = await family_ref_viewer(db, user, key_owner) if recognised else FamilyRefViewer(None, None, None)
    families = await _family_refs(db, recognised, user_families, viewer, user or key_owner)
    generic = catalog.generic_family_ids()

    out: list[OnlinePrinter] = []
    for printer in printers:
        snapshot = snapshots[printer.id]
        model = snapshot.model or normalize_model_name(printer.model)
        filaments: list[LoadedFilament] = []
        for source in snapshot.sources:
            material = (source.material or "").strip()
            fid = (source.variant or "").strip()
            family_id = fid if fid in recognised else None
            family = (catalog.get_family(fid) or user_families.get(fid)) if family_id else None
            family_type = support_slot_type(family.filament_type, fid) if family else ""
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
