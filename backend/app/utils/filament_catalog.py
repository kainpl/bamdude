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
    all_presets: list[CatalogPreset] = []
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
        all_presets.append(preset)
        by_setting.setdefault(preset.setting_id, preset)
        by_name[preset.name] = preset
        by_family.setdefault(preset.filament_id, []).append(preset)
    # Base aliases in a SECOND pass, into still-free keys only: an exact
    # setting id must never lose to a stripped alias. Bambu reused the
    # GFSR99 space — the bare id is the legacy Generic TPU (GFU99) while
    # most GFSR99_NN variants are Generic EVA (GFR99); one pass let EVA's
    # alias clobber the exact TPU key (rows are name-sorted, EVA first) and
    # a tray configured as Generic TPU resolved to EVA everywhere.
    for preset in all_presets:
        by_setting.setdefault(_base_setting_id(preset.setting_id), preset)
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


@cache
def generic_family_ids() -> frozenset[str]:
    """The "standard shelf": every Generic-vendor family across ecosystems.
    The mine-scope browse list always carries these — the first spool of a
    new material needs its generic before anything references it."""
    ids = set()
    for eco in _ECOSYSTEMS:
        for fam in _load(eco).families.values():
            if (fam.vendor or "").strip().lower() == "generic":
                ids.add(fam.filament_id)
    return frozenset(ids)


def generic_family_for_material(material: str) -> CatalogFamily | None:
    """The Generic family of a material's BASE — the name cut at the first hyphen
    or space, so ``PETG-CF`` answers Generic PETG-CF but ``PLA-AERO`` answers
    Generic PLA. That is a base to inherit from, not the material: a slot is
    configured through :func:`family_for_material`, which never changes the type."""
    mat = (material or "").strip().upper()
    if not mat:
        return None
    for candidate in (mat, mat.split("-")[0].split(" ")[0]):
        for eco in _ECOSYSTEMS:
            for fam in _load(eco).families.values():
                if fam.alias.upper() == f"GENERIC {candidate}":
                    return fam
    return None


def _all_families() -> list[CatalogFamily]:
    return [fam for eco in _ECOSYSTEMS for fam in _load(eco).families.values()]


@cache
def _catalog_types() -> frozenset[str]:
    return frozenset((fam.filament_type or "").upper() for fam in _all_families() if fam.filament_type)


def _words(value: str) -> str:
    return " ".join(value.upper().split())


def _family_of_type(filament_type: str) -> CatalogFamily | None:
    """A system family of exactly ``filament_type``: its Generic one (the plain
    ``Generic <type>`` before ``Generic <type> Silk``), else the catalogue's own —
    Bambu Lab first, then by id, so the answer never depends on file order."""
    same = [fam for fam in _all_families() if (fam.filament_type or "").upper() == filament_type]
    generics = [fam for fam in same if (fam.vendor or "").strip().lower() == "generic"]
    if generics:
        exact = [fam for fam in generics if _words(fam.alias) == f"GENERIC {filament_type}"]
        return exact[0] if exact else min(generics, key=lambda fam: (len(fam.alias), fam.filament_id))
    if same:
        return min(same, key=lambda fam: ((fam.vendor or "").strip().lower() != "bambu lab", fam.filament_id))
    return None


def material_type(material: str) -> str | None:
    """The catalogue filament type a free-text material name states, or None.

    Adjacent words are joined with a hyphen and the longest join that IS a type
    wins — "PLA Aero" is PLA-AERO, "PolyTerra PLA" and "PLA Matte" are PLA. The
    join has to be a type exactly: a hyphenated name the catalogue does not know
    ("ASA-GF") is not reduced to its base, because the base is another material.
    A trailing ``+`` is a qualifier, and the routing equivalences apply
    (PA12-CF is PA-CF), so what this answers is what the queue will compare.
    """
    from backend.app.utils.filament_types import canonical_filament_type

    tokens = [token.rstrip("+") for token in _words(material or "").split(" ")]
    tokens = [token for token in tokens if token]
    types = _catalog_types()
    for length in range(len(tokens), 0, -1):
        for start in range(len(tokens) - length + 1):
            candidate = canonical_filament_type("-".join(tokens[start : start + length]))
            if candidate in types:
                return candidate
    return None


def family_for_material(material: str) -> CatalogFamily | None:
    """The system family a bare material name stands for — always of THAT material.

    For a spool that names no family (a Spoolman spool without a linked profile,
    a quick-added or legacy one) the slot still has to be told a profile. The old
    answer, :func:`generic_family_for_material`, cut the name to its base, so
    PLA-AERO went out as Generic PLA and the slot said PLA: routing compares that
    type and nothing else under base-material matching, and AMS Backup groups by
    the profile (upstream #2902). Here, in order:

    1. a family whose name the material IS (``Generic PETG HF`` for "PETG HF" or
       "PETG-HF", ``Bambu PLA Aero`` for itself);
    2. the family of the type the name states (:func:`material_type`) — its
       Generic one, else the catalogue's own (PLA-AERO → Bambu PLA Aero);
    3. None — the name is no known type. The caller decides what then; the slot
       plan keeps the spool's own type and borrows the base's profile.
    """
    words = _words(material or "")
    if not words:
        return None
    names = {words, words.replace("-", " ")}
    names |= {f"GENERIC {name}" for name in names}
    for fam in _all_families():
        if _words(fam.alias) in names:
            return fam
    filament_type = material_type(words)
    return _family_of_type(filament_type) if filament_type else None


@cache
def all_printer_names(ecosystem: str = "bambu") -> list[str]:
    """Every printer PROFILE name the catalog knows ("Bambu Lab P1S 0.4
    nozzle", ...) — the BS-native way to target a preset at a printer.
    Sorted + deduplicated; drives the authoring dialog's printer list."""
    names: set[str] = set()
    for preset in _load(ecosystem).presets_by_setting.values():
        names.update(preset.compatible_printers)
    return sorted(names)


@cache
def printers_for_preset_suffix(suffix: str, ecosystem: str = "bambu") -> frozenset[str]:
    """The printer profiles a preset named "… @<suffix>" is made for.

    A slicer preset names its printer after an "@" — "Bambu PLA Basic @BBL A1M",
    "… @BBL X2D 0.4 nozzle" — and a user preset saved from one keeps the same
    suffix. The catalogue's own presets carrying that suffix say which printers
    it means. Empty when no system preset uses it.
    """
    wanted = f"@{suffix.strip()}".casefold()
    printers: set[str] = set()
    for preset in _load(ecosystem).presets_by_name.values():
        if preset.name.casefold().endswith(wanted):
            printers.update(preset.compatible_printers)
    return frozenset(printers)
