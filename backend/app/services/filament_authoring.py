"""Authoring of custom filament families (spec B) — BamDude's analog of
BS's Create Filament dialog: vendor+type+serial -> a P-hash family + one
root LocalPreset per chosen printer, absorbed into the spec-A mirrors so
slots / K-profiles / slicing see it like any family.

BS parity (temp/references/BambuStudio/src/slic3r/GUI/CreatePresetsDialog.cpp):
- special_key strip set, vendor refusals, the fixed filament-type list;
- get_filament_id: pre-'@' name match adopts the existing filament_id,
  otherwise "P"+md5(name)[:7] (logged-out form — no user-id salt: the
  family lives in BamDude and on printers, and BS's own name-search
  convergence makes the hash push-compatible), collision with a different
  name re-hashes with a timestamp appended.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset
from backend.app.utils import filament_catalog as catalog

logger = logging.getLogger(__name__)

# BS CreatePresetsDialog.cpp:43 — order preserved, the literal duplicate
# "PETG" removed.
FILAMENT_TYPES = [
    "PLA", "PLA+", "PLA Tough", "PETG", "ABS", "ASA", "FLEX", "HIPS", "PA", "PACF",
    "NYLON", "PVA", "PC", "PCABS", "PCTG", "PCCF", "PP", "PEI", "PET",
    "PETGCF", "PTBA", "PTBA90A", "PEEK", "TPU93A", "TPU75D", "TPU", "TPU-AMS",
    "TPU92A", "TPU98A", "Misc", "TPE", "GLAZE", "Nylon", "CPE", "METAL", "ABST",
    "Carbon Fiber",
]  # fmt: skip

_SPECIAL_KEYS = set("\n\t\r\v@;")  # BS special_key set


class AuthoringError(ValueError):
    """Caller-facing authoring failure (400 at the route layer)."""


class FamilyInUseError(AuthoringError):
    """Deletion refused: spools / calibrations still reference the id (409)."""

    def __init__(self, spools: int, calibrations: int):
        super().__init__(f"family is referenced by {spools} spool(s) and {calibrations} calibration(s)")
        self.spools = spools
        self.calibrations = calibrations


def strip_special_keys(s: str) -> str:
    return "".join(c for c in s if c not in _SPECIAL_KEYS)


def validate_vendor(vendor: str) -> str:
    v = strip_special_keys(vendor or "").strip()
    if not v:
        raise AuthoringError("vendor is required")
    # BS compares case-sensitively; we refuse any casing — "bambu PLA x"
    # differing from the reserved vendor only by case would mislead.
    if v.lower() in ("bambu", "generic"):
        raise AuthoringError(f'vendor "{v}" is reserved')
    if v.isdigit():
        raise AuthoringError("vendor cannot be digits only")
    return v


def build_family_name(vendor: str, filament_type: str, serial: str) -> str:
    if filament_type not in FILAMENT_TYPES:
        raise AuthoringError(f"unknown filament type {filament_type!r}")
    s = strip_special_keys(serial or "").strip()
    if not s:
        raise AuthoringError("serial is required")
    return f"{validate_vendor(vendor)} {filament_type} {s}"


def _alias_of(name: str) -> str:
    return name.split("@")[0].strip() if "@" in name else name.strip()


async def _known_names(db: AsyncSession) -> dict[str, set[str]]:
    """filament_id -> pre-'@' names, across catalog + mirrors + families —
    BS get_filament_id walks system+user preset bundles the same way."""
    known: dict[str, set[str]] = {}
    for fam in catalog.search_families("", 100000):
        known.setdefault(fam.filament_id, set()).add(fam.alias)
    for row in (await db.execute(select(UserFilamentPreset))).scalars():
        if row.family_filament_id:
            known.setdefault(row.family_filament_id, set()).add(_alias_of(row.name))
    for fam in (await db.execute(select(UserFilamentFamily))).scalars():
        known.setdefault(fam.filament_id, set()).add(fam.alias)
    return known


async def mint_filament_id(db: AsyncSession, family_name: str) -> tuple[str, bool]:
    """Return ``(filament_id, attached)``. A known name adopts its existing
    id (BS convergence / spec §1 dedup); otherwise mint the logged-out
    P-hash, re-hashing while the id is taken by a different name."""
    known = await _known_names(db)
    for fid, names in known.items():
        if family_name in names:
            return fid, True
    fid = "P" + hashlib.md5(family_name.encode("utf-8")).hexdigest()[:7]
    while fid in known:  # taken by a DIFFERENT name (same name returned above)
        salted = f"{family_name}{datetime.now(timezone.utc).isoformat()}"
        fid = "P" + hashlib.md5(salted.encode("utf-8")).hexdigest()[:7]
    return fid, False


# ---------------------------------------------------------------------------
# Content clones + lifecycle (spec B §2–§3)
# ---------------------------------------------------------------------------

_STUB_KEYS = {"name", "inherits", "from", "type"}


@dataclass
class ClonedRoot:
    printer_id: int
    printer_name: str | None
    local_preset_id: int | None
    preset_name: str | None
    error: str | None = None


@dataclass
class CreateFamilyResult:
    filament_id: str
    name: str
    attached: bool
    roots: list[ClonedRoot] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _scalar(value):
    if isinstance(value, list):
        value = value[0] if value else None
    return value


def _printer_context(printer) -> tuple[str | None, str]:
    """(model, nozzle) — nozzle from live state when the printer is
    connected, "0.4" otherwise (same sourcing as the slot paths)."""
    nozzle = "0.4"
    try:
        from backend.app.services.printer_manager import printer_manager

        info = printer_manager.get_printer(printer.id)
        state = getattr(getattr(info, "mqtt_client", None), "state", None) if info else None
        nozzles = getattr(state, "nozzles", None) or []
        nd = getattr(nozzles[0], "nozzle_diameter", None) if nozzles else None
        if nd:
            nozzle = str(nd)
    except Exception:  # noqa: BLE001 — authoring must work with the farm offline
        pass
    return printer.model, nozzle


def _base_for_printer(filament_type: str, model: str | None, nozzle: str):
    """(base preset, printer display name) for the 'from type' clone —
    picked from the catalog exactly like slot assignment picks its preset."""
    from backend.app.services.slot_assignment import _candidate_printer_names, _pick_preset

    candidates = _candidate_printer_names(model, nozzle)
    fam = catalog.generic_family_for_material(filament_type)
    preset = _pick_preset(fam.filament_id, candidates) if fam else None
    printer_name = None
    if preset:
        printer_name = next((n for n in candidates if n in preset.compatible_printers), None)
    if printer_name is None and candidates:
        printer_name = candidates[0]
    return preset, printer_name


async def _resolve_bundled_content(db: AsyncSession, base_name: str) -> dict | None:
    """Full content of a bundled preset via the sidecar's resolver
    (spec §2 'from type'). None = sidecar missing/old/failed."""
    from backend.app.api.routes.slicer_presets import _resolve_slicer_api_url
    from backend.app.services.slicer_api import SlicerApiService, SlicerApiUnavailableError

    api_url = await _resolve_slicer_api_url(db)
    if not api_url:
        return None
    stub = json.dumps({"name": base_name, "inherits": base_name, "from": "system", "type": "filament"})
    try:
        async with SlicerApiService(base_url=api_url) as svc:
            resolved = await svc.resolve_profile(stub, "filament")
    except SlicerApiUnavailableError:
        return None
    return resolved.values if resolved.reason == "ok" else None


async def _resolve_source_preset(db: AsyncSession, user, source: str, source_id: str) -> dict | None:
    """Full content for 'from an existing preset' — flattened, because the
    clone becomes a root (inherits cleared)."""
    from backend.app.schemas.slicer import PresetRef
    from backend.app.services.orca_profiles import fetch_and_cache_base_profile, resolve_preset as flatten
    from backend.app.services.preset_resolver import resolve_preset_ref

    try:
        content = json.loads(await resolve_preset_ref(db, user, PresetRef(source=source, id=source_id), "filament"))
    except Exception as e:  # noqa: BLE001 — per-printer best effort, error lands in the root row
        logger.info("authoring: source preset %s/%s unavailable: %s", source, source_id, e)
        return None
    inherits = _scalar(content.get("inherits"))
    if inherits:
        if set(content) <= _STUB_KEYS:  # a standard-tier stub — content lives in the sidecar
            return await _resolve_bundled_content(db, str(inherits))
        base = await fetch_and_cache_base_profile(str(inherits), "filament", db)
        if base is None:
            return None  # dangling diff — clearing inherits would break it
        content = {**(await flatten(base, "filament", db)), **content}
    return content


def _apply_root_overrides(
    content: dict, *, family_name: str, printer_name: str, fid: str, vendor: str, filament_type: str
) -> dict:
    """BS clone_presets_for_filament parity (spec §2)."""
    out = dict(content)
    name = f"{family_name} @{printer_name}"
    out["name"] = name
    out["filament_id"] = fid
    out["filament_vendor"] = [vendor]
    out["filament_type"] = [filament_type]
    out["compatible_printers"] = [printer_name]
    out["from"] = "User"
    out["type"] = "filament"
    out["filament_settings_id"] = [name]
    out.pop("inherits", None)  # every clone is a root
    out.pop("setting_id", None)  # cloud identity never travels into a clone
    return out


async def _clone_roots(
    db: AsyncSession,
    *,
    fid: str,
    family_name: str,
    vendor: str,
    filament_type: str,
    printer_ids: list[int],
    source_mode: str,
    source: str | None,
    source_id: str | None,
    user,
) -> tuple[list[ClonedRoot], list[str]]:
    from backend.app.models.local_preset import LocalPreset
    from backend.app.models.printer import Printer
    from backend.app.services.filament_preset_sync import absorb_local_preset
    from backend.app.services.orca_profiles import extract_core_fields

    roots: list[ClonedRoot] = []
    warnings: list[str] = []
    for pid in printer_ids:
        printer = await db.get(Printer, pid)
        if printer is None:
            roots.append(ClonedRoot(pid, None, None, None, "printer not found"))
            continue
        model, nozzle = _printer_context(printer)
        base_preset, printer_name = _base_for_printer(filament_type, model, nozzle)
        if printer_name is None:
            roots.append(ClonedRoot(pid, None, None, None, f"no BS printer name for model {model!r}"))
            warnings.append(f"{printer.name}: unknown printer model — preset skipped")
            continue
        if source_mode == "preset" and source and source_id:
            content = await _resolve_source_preset(db, user, source, source_id)
            failure = "source preset content unavailable"
        else:
            content = await _resolve_bundled_content(db, base_preset.name) if base_preset else None
            failure = "slicer sidecar unavailable — created without content"
        if content is None:
            roots.append(ClonedRoot(pid, printer_name, None, None, failure))
            warnings.append(f"{printer.name}: {failure}")
            continue
        blob = _apply_root_overrides(
            content,
            family_name=family_name,
            printer_name=printer_name,
            fid=fid,
            vendor=vendor,
            filament_type=filament_type,
        )
        dupe = (await db.execute(select(LocalPreset).where(LocalPreset.name == blob["name"]))).scalars().first()
        if dupe is not None:
            roots.append(ClonedRoot(pid, printer_name, dupe.id, blob["name"], "preset already exists"))
            continue
        preset = LocalPreset(
            name=blob["name"],
            preset_type="filament",
            source="authored",
            setting=json.dumps(blob),
            **extract_core_fields(blob),
        )
        db.add(preset)
        await db.flush()
        await absorb_local_preset(db, preset)
        roots.append(ClonedRoot(pid, printer_name, preset.id, blob["name"]))
    return roots, warnings


async def _ensure_family_row(db: AsyncSession, *, fid: str, family_name: str, vendor: str, filament_type: str) -> None:
    if catalog.get_family(fid):
        return  # attached to a system family — nothing to add
    existing = (
        (await db.execute(select(UserFilamentFamily).where(UserFilamentFamily.filament_id == fid))).scalars().first()
    )
    if existing is not None:
        existing.orphaned = False
        return
    db.add(
        UserFilamentFamily(
            filament_id=fid,
            ecosystem="local",
            alias=family_name,
            vendor=vendor,
            filament_type=filament_type,
            origin="authored",
        )
    )


async def create_family(
    db: AsyncSession,
    *,
    vendor: str,
    filament_type: str,
    serial: str,
    printer_ids: list[int],
    source_mode: str = "type",
    source: str | None = None,
    source_id: str | None = None,
    user=None,
) -> CreateFamilyResult:
    v = validate_vendor(vendor)
    family_name = build_family_name(vendor, filament_type, serial)
    fid, attached = await mint_filament_id(db, family_name)
    await _ensure_family_row(db, fid=fid, family_name=family_name, vendor=v, filament_type=filament_type)
    roots, warnings = await _clone_roots(
        db,
        fid=fid,
        family_name=family_name,
        vendor=v,
        filament_type=filament_type,
        printer_ids=printer_ids,
        source_mode=source_mode,
        source=source,
        source_id=source_id,
        user=user,
    )
    await db.commit()
    return CreateFamilyResult(filament_id=fid, name=family_name, attached=attached, roots=roots, warnings=warnings)


async def add_printers_to_family(
    db: AsyncSession,
    *,
    filament_id: str,
    printer_ids: list[int],
    source_mode: str = "type",
    source: str | None = None,
    source_id: str | None = None,
    user=None,
) -> CreateFamilyResult:
    """BS clone_presets_for_printer shape: one more root with the same id."""
    fam = (
        (
            await db.execute(
                select(UserFilamentFamily).where(
                    UserFilamentFamily.filament_id == filament_id,
                    UserFilamentFamily.origin == "authored",
                )
            )
        )
        .scalars()
        .first()
    )
    if fam is None:
        raise AuthoringError("not an authored family")
    if not fam.filament_type or fam.filament_type not in FILAMENT_TYPES:
        raise AuthoringError("family has no usable filament type")
    roots, warnings = await _clone_roots(
        db,
        fid=filament_id,
        family_name=fam.alias,
        vendor=fam.vendor or "",
        filament_type=fam.filament_type,
        printer_ids=printer_ids,
        source_mode=source_mode,
        source=source,
        source_id=source_id,
        user=user,
    )
    await db.commit()
    return CreateFamilyResult(filament_id=filament_id, name=fam.alias, attached=True, roots=roots, warnings=warnings)


async def delete_family(db: AsyncSession, *, filament_id: str, also_cloud: bool = False, user=None) -> dict:
    """Refuse while referenced (spec §3 — BS 'a base with children cannot be
    deleted' mirror); otherwise remove authored roots + mirrors + the row.
    ``also_cloud`` deletes pushed copies first (best-effort).
    """
    from sqlalchemy import func as sa_func

    from backend.app.models.filament_calibration import FilamentCalibration
    from backend.app.models.local_preset import LocalPreset
    from backend.app.models.spool import Spool

    fam = (
        (
            await db.execute(
                select(UserFilamentFamily).where(
                    UserFilamentFamily.filament_id == filament_id,
                    UserFilamentFamily.origin == "authored",
                )
            )
        )
        .scalars()
        .first()
    )
    if fam is None:
        raise AuthoringError("not an authored family")
    spools = (await db.execute(select(sa_func.count()).where(Spool.filament_family_id == filament_id))).scalar_one()
    cals = (
        await db.execute(select(sa_func.count()).where(FilamentCalibration.filament_id == filament_id))
    ).scalar_one()
    if spools or cals:
        raise FamilyInUseError(spools=spools, calibrations=cals)

    mirrors = (
        (
            await db.execute(
                select(UserFilamentPreset).where(
                    UserFilamentPreset.family_filament_id == filament_id,
                    UserFilamentPreset.source == "local",
                )
            )
        )
        .scalars()
        .all()
    )
    cloud_deleted = await _delete_pushed_copies(db, mirrors, user) if also_cloud else 0
    presets_deleted = 0
    for row in mirrors:
        if row.local_preset_id is not None:
            preset = await db.get(LocalPreset, row.local_preset_id)
            if preset is not None and preset.source == "authored":
                await db.delete(preset)
                presets_deleted += 1
        await db.delete(row)
    await db.delete(fam)
    await db.commit()
    return {"presets_deleted": presets_deleted, "cloud_deleted": cloud_deleted}


async def _delete_pushed_copies(db: AsyncSession, mirrors, user) -> int:
    """Best-effort ``delete_setting`` per pushed preset (spec §5)."""
    from backend.app.services.filament_preset_sync import _build_bambu_cloud

    pushed = [m for m in mirrors if m.pushed_cloud_id]
    if not pushed:
        return 0
    cloud = await _build_bambu_cloud(db, user)
    if cloud is None or not cloud.is_authenticated:
        if cloud is not None:
            await cloud.close()
        return 0
    deleted = 0
    try:
        for row in pushed:
            try:
                await cloud.delete_setting(row.pushed_cloud_id)
                deleted += 1
            except Exception as e:  # noqa: BLE001 — best-effort per spec §5
                logger.info("cloud delete of %s failed: %s", row.pushed_cloud_id, e)
    finally:
        await cloud.close()
    return deleted
