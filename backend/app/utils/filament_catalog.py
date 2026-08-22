"""In-memory view over backend/app/data/filament_catalog/{bambu,orca}.json —
the distilled SYSTEM tier of the filament catalog (see the folder README and
docs/superpowers/specs/2026-08-22-filament-family-catalog-design.md). Identity
only; preset *content* stays where it lives today (sidecar / clouds / local
presets).

Lookup precedence bambu-then-orca: GF* ids coincide across ecosystems (Orca
mirrors BBL), so the first hit is canonical.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import cache
from pathlib import Path

logger = logging.getLogger(__name__)

_CATALOG_DIR = Path(__file__).resolve().parent.parent / "data" / "filament_catalog"
_ECOSYSTEMS = ("bambu", "orca")


@dataclass(frozen=True)
class CatalogFamily:
    filament_id: str
    alias: str
    vendor: str | None
    filament_type: str | None
    is_support: bool


@dataclass(frozen=True)
class CatalogPreset:
    name: str
    setting_id: str
    filament_id: str
    ecosystem: str
    compatible_printers: tuple[str, ...]
    nozzle_temp_min: int | None
    nozzle_temp_max: int | None


@dataclass(frozen=True)
class _Indexed:
    families: dict[str, CatalogFamily]  # filament_id -> family
    presets_by_setting: dict[str, CatalogPreset]  # versioned AND base setting_id forms
    presets_by_name: dict[str, CatalogPreset]
    presets_by_family: dict[str, tuple[CatalogPreset, ...]]


def _base_setting_id(setting_id: str) -> str:
    return setting_id.split("_")[0] if "_" in setting_id else setting_id


@cache
def _load(ecosystem: str) -> _Indexed:
    path = _CATALOG_DIR / f"{ecosystem}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.error("filament catalog %s unreadable: %s", path, e)
        return _Indexed({}, {}, {}, {})

    families = {
        f["filament_id"]: CatalogFamily(
            filament_id=f["filament_id"],
            alias=f["alias"],
            vendor=f.get("vendor"),
            filament_type=f.get("filament_type"),
            is_support=bool(f.get("is_support")),
        )
        for f in data.get("families", [])
    }
    by_setting: dict[str, CatalogPreset] = {}
    by_name: dict[str, CatalogPreset] = {}
    by_family: dict[str, list[CatalogPreset]] = {}
    for p in data.get("presets", []):
        low, high = (p.get("nozzle_temp") or [None, None])[:2]
        preset = CatalogPreset(
            name=p["name"],
            setting_id=p["setting_id"],
            filament_id=p["filament_id"],
            ecosystem=ecosystem,
            compatible_printers=tuple(p.get("compatible_printers") or ()),
            nozzle_temp_min=low,
            nozzle_temp_max=high,
        )
        by_setting.setdefault(preset.setting_id, preset)
        # Base form: rows are name-sorted, so the first writer wins deterministically.
        by_setting.setdefault(_base_setting_id(preset.setting_id), preset)
        by_name[preset.name] = preset
        by_family.setdefault(preset.filament_id, []).append(preset)
    return _Indexed(
        families,
        by_setting,
        by_name,
        {k: tuple(v) for k, v in by_family.items()},
    )


def get_family(filament_id: str) -> CatalogFamily | None:
    for eco in _ECOSYSTEMS:
        fam = _load(eco).families.get(filament_id)
        if fam:
            return fam
    return None


def preset_for_setting_id(setting_id: str) -> CatalogPreset | None:
    if not setting_id:
        return None
    for key in (setting_id, _base_setting_id(setting_id)):
        for eco in _ECOSYSTEMS:
            preset = _load(eco).presets_by_setting.get(key)
            if preset:
                return preset
    return None


def family_for_setting_id(setting_id: str) -> CatalogFamily | None:
    preset = preset_for_setting_id(setting_id)
    return get_family(preset.filament_id) if preset else None


def preset_by_name(name: str, ecosystem: str) -> CatalogPreset | None:
    return _load(ecosystem).presets_by_name.get(name)


def presets_for_family(filament_id: str) -> list[CatalogPreset]:
    seen: dict[str, CatalogPreset] = {}
    for eco in _ECOSYSTEMS:
        for preset in _load(eco).presets_by_family.get(filament_id, ()):
            seen.setdefault(preset.name, preset)
    return sorted(seen.values(), key=lambda p: p.name)


def search_families(q: str, limit: int = 50) -> list[CatalogFamily]:
    needle = (q or "").strip().lower()
    out: dict[str, CatalogFamily] = {}
    for eco in _ECOSYSTEMS:
        for fam in _load(eco).families.values():
            if fam.filament_id in out:
                continue
            hay = f"{fam.alias} {fam.vendor or ''} {fam.filament_type or ''} {fam.filament_id}".lower()
            if needle in hay:
                out[fam.filament_id] = fam
    return sorted(out.values(), key=lambda f: f.alias)[:limit]


def generic_family_for_material(material: str) -> CatalogFamily | None:
    mat = (material or "").strip().upper()
    if not mat:
        return None
    for candidate in (mat, mat.split("-")[0].split(" ")[0]):
        for eco in _ECOSYSTEMS:
            for fam in _load(eco).families.values():
                if fam.alias.upper() == f"GENERIC {candidate}":
                    return fam
    return None
