"""The ONE builder of ams_filament_setting payloads (spec A §5.2). Both
routes (spool assign, manual slot configure) feed a family in and publish
what comes out. BS payload parity: tray_info_idx = family id, versioned
setting_id from the catalog, temps from the preset for THIS printer,
cols/ctype for multi-colour, NO tray_sub_brands. Custom (P*) families are
gated on the device's support_user_preset flag and degrade to the generic
family of the same type, loudly.

A family the user created (a P-hash the catalogue does not know) sends the
user's own preset of it made for THIS printer model and nozzle — the system
families' per-printer pick, done for the user's family from its cloud mirrors
(audit D2, upstream a7b56333, without their per-spool override table: the
spool's identity is its family, and the mirrors already say which printer
each variant is for).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.services.filament_identity import (
    bambu_preset_id,
    family_user_presets,
    resolve_raw,
    resolve_spool,
    resolve_tray,
)
from backend.app.utils import filament_catalog as catalog
from backend.app.utils.filament_types import SUPPORT_SLOT_TYPES, filament_types_compatible, support_slot_type
from backend.app.utils.printer_models import PRINTER_MODEL_MAP

logger = logging.getLogger(__name__)


@dataclass
class SlotAssignmentPlan:
    tray_info_idx: str
    setting_id: str
    tray_type: str
    tray_color: str
    cols: list[str] = field(default_factory=list)
    ctype: int = 0
    nozzle_temp_min: int = 200
    nozzle_temp_max: int = 240
    warnings: list[str] = field(default_factory=list)
    # A CODE, for the caller to tell the operator: the filament that was NAMED
    # could not be used as named and a generic profile went out instead —
    # ``family_unresolved`` (nobody knows that id) or ``user_preset_unsupported``
    # (the printer takes no user presets). None when nothing named was replaced:
    # a spool that names no family simply has its generic. ``warnings`` above
    # stays the log's prose.
    degraded: str | None = None
    # The family came from the preset stored on a Spoolman spool — neither a
    # linked calibration nor the material's stand-in. The callers that put a
    # slot's previous K index back read it: that index was chosen under whatever
    # the slot held before, and is this family's only when the printer says so.
    from_stored_preset: bool = False


def _candidate_printer_names(printer_model: str | None, nozzle_diameter: str) -> list[str]:
    """BS preset names are '<display name> <d> nozzle'. Our DB stores the
    normalized short model, so invert PRINTER_MODEL_MAP — every display
    variant that normalizes to this short name is a candidate; the catalog's
    own compatible_printers decides which one is real."""
    if not printer_model:
        return []
    short = printer_model.strip().upper()
    return [
        f"{display} {nozzle_diameter} nozzle"
        for display, mapped in PRINTER_MODEL_MAP.items()
        if mapped.upper() == short
    ]


def _stand_in(material: str) -> tuple[catalog.CatalogFamily | None, str]:
    """The system profile a slot is told about for ``material``, and the type it gets.

    The slot is configured from the spool, so its type is the spool's (upstream
    #2902). A family of exactly that type answers when the catalogue has one —
    its type is then the catalogue's spelling ("PLA Aero" → PLA-AERO). When it
    has none ("ASA-GF"), the base material's generic lends the profile, for its
    preset and temperatures, and the type is written as the spool gives it: the
    old answer took the generic's type too, and the slot said ASA.
    """
    typed_support = {"PLA-S": "GFS00", "PA-S": "GFS01"}.get(material.strip().upper())
    if typed_support:
        return catalog.get_family(typed_support), material.strip().upper()

    family = catalog.family_for_material(material)
    # Spoolman catalogs can omit the vendor prefix from a Bambu product name.
    if material.strip().casefold().startswith("support "):
        alias = f"Bambu {material.strip()}"
        named = catalog.family_for_material(alias)
        if named and named.alias.casefold() == alias.casefold():
            family = named
    if family is not None:
        return family, support_slot_type(family.filament_type or material.strip(), family.filament_id)
    # Nylon / PA6 / PA12 borrow Generic PA inside ``generic_family_for_material``
    # (owner decision Р26); the type stays the spool's — the same rule as above.
    return catalog.generic_family_for_material(material), material.strip()


def _preset_is_of_material(preset_type: str | None, material: str | None) -> bool:
    """Would the preset tell the slot the type the spool's material gives it?

    The spool's side is what :func:`_stand_in` would write for that material —
    the catalogue's own reading of the name ("PETG HF" and "PLA+" are PETG and
    PLA) — and the two are compared as routing compares types. A side that
    states no type has nothing to contradict the other with.
    """
    if not (material or "").strip() or not (preset_type or "").strip():
        return True
    _, spool_type = _stand_in(material)
    return filament_types_compatible(preset_type, spool_type) or (
        preset_type in SUPPORT_SLOT_TYPES and filament_types_compatible(preset_type.removesuffix("-S"), spool_type)
    )


def _pick_preset(family_id: str, candidates: list[str]) -> catalog.CatalogPreset | None:
    presets = catalog.presets_for_family(family_id)
    for preset in presets:
        if any(name in preset.compatible_printers for name in candidates):
            return preset
    return presets[0] if presets else None


def _user_preset_printers(row) -> set[str]:
    """The printer profiles a user preset was made for, as far as it says.

    Its parent first (``base_ref``: the system preset's setting id for a Bambu
    mirror, its name for an Orca one) — structural, where a name is typed. Then
    the "@<printer>" part of its own name: a printer profile outright
    ("@Bambu Lab X2D 0.4 nozzle", how the slicer names a created filament) or a
    preset suffix the catalogue's presets share ("@BBL A1M").
    """
    base = (row.base_ref or "").strip()
    if base:
        parent = catalog.preset_for_setting_id(base) or catalog.preset_by_name(base, row.ecosystem or "bambu")
        if parent is not None and parent.compatible_printers:
            return {p.casefold() for p in parent.compatible_printers}
    name = row.name or ""
    if "@" not in name:
        return set()
    suffix = name.split("@", 1)[1].strip()
    if suffix.casefold() in {p.casefold() for p in catalog.all_printer_names()}:
        return {suffix.casefold()}
    return {p.casefold() for p in catalog.printers_for_preset_suffix(suffix)}


def _fits(row, candidates: list[str]) -> bool | None:
    """Is this user preset made for the printer? ``None`` when nothing says."""
    printers = _user_preset_printers(row)
    if not printers or not candidates:
        return None
    return bool(printers & {c.casefold() for c in candidates})


_NOZZLE_SIZES = ("0.2", "0.4", "0.6", "0.8")


def _user_presets_for_printer(presets: list, printer_model: str | None, nozzle_diameter: str):
    """``(preset to name, preset whose temperatures to use)`` for this printer (audit D2).

    ``presets`` are the family's own, newest first. Named only when one is made
    for exactly this printer and nozzle: a preset of another printer profile is
    what upstream's reporter got on his second machine, and not what the slot
    should say. Temperatures describe the filament, so without an exact variant
    they come from this model's variant for another nozzle, else from the newest
    — better than the generic 200–240 °C.
    """
    exact = _candidate_printer_names(printer_model, nozzle_diameter)
    named = next((row for row in presets if _fits(row, exact)), None)
    if named is not None:
        return named, named
    same_model = [name for size in _NOZZLE_SIZES for name in _candidate_printer_names(printer_model, size)]
    temps = next((row for row in presets if _fits(row, same_model)), None) or (presets[0] if presets else None)
    return None, temps


async def build_slot_assignment(
    db: AsyncSession,
    *,
    spool=None,
    family_id: str | None = None,
    stored_preset: str | None = None,
    preset_setting_id: str | None = None,
    printer_model: str | None = None,
    nozzle_diameter: str = "0.4",
    supports_user_preset: bool = True,
    material_override: str | None = None,
    color_rgba: str = "FFFFFFFF",
    extra_colors: str | None = None,
    temp_overrides: tuple[int | None, int | None] = (None, None),
) -> SlotAssignmentPlan:
    warnings: list[str] = []
    degraded: str | None = None
    # What the caller NAMED, before any resolution — to tell "named and not
    # usable" (said to the operator) from "names nothing" (its generic is right).
    named_family = (family_id or getattr(spool, "filament_family_id", None) or "").strip()

    resolved = None
    if spool is not None and family_id is None:
        # The spool's own resolution is used AS the resolved identity — it may
        # carry the mirrored cloud preset's setting_id and temps, which a
        # re-resolution through the bare family id would lose.
        resolved = await resolve_spool(db, spool)
        family_id = resolved.family.filament_id if resolved.family else None
        material_override = material_override or getattr(spool, "material", None)
        color_rgba = getattr(spool, "rgba", None) or color_rgba
        extra_colors = getattr(spool, "extra_colors", None) or extra_colors
        temp_overrides = (
            getattr(spool, "nozzle_temp_min", None) or temp_overrides[0],
            getattr(spool, "nozzle_temp_max", None) or temp_overrides[1],
        )

    if resolved is None or not resolved.family:
        resolved = await resolve_tray(db, family_id) if family_id else None
    # A caller without a Spool row (Spoolman) names the family it has — the
    # linked calibration's — and the preset stored ON the spool. The family
    # wins; the stored preset answers only where there is none, in place of
    # the stand-in below: a Bambu PETG HF spool with no calibration on this
    # printer went out as Generic PETG (upstream 5e3a2f278, issue #3433).
    # ⚠️ Only a preset of the spool's OWN material: in Spoolman the material is
    # edited outside BamDude, and a preset left behind by another filament
    # (PLA Basic on a spool that is now PETG) would tell the slot PLA.
    from_stored_preset = False
    if (resolved is None or not resolved.family) and stored_preset:
        candidate = await resolve_raw(db, stored_preset)
        if candidate.family and _preset_is_of_material(
            support_slot_type(candidate.filament_type, candidate.family.filament_id),
            material_override,
        ):
            resolved, from_stored_preset = candidate, True
        elif candidate.family:
            warnings.append(
                f"stored preset {candidate.family.filament_id} is {candidate.filament_type!r}, "
                f"the spool is {material_override!r}; preset ignored"
            )
    fam_type = (resolved.filament_type if resolved and resolved.family else None) or (material_override or "")

    # No family -> a system profile for the spool's material, warning-logged.
    # ⚠️ The TYPE is the spool's: only the profile is borrowed (_stand_in).
    if not resolved or not resolved.family:
        stand_in, fam_type = _stand_in(material_override or "")
        if stand_in is None:
            raise ValueError(f"no filament family resolvable (family_id={family_id!r}, material={material_override!r})")
        warnings.append(f"family unresolved; using {stand_in.filament_id} for {fam_type!r}")
        if named_family:
            degraded = "family_unresolved"
        family_id = stand_in.filament_id
    else:
        family_id = resolved.family.filament_id
        fam_type = support_slot_type(fam_type, family_id)
        # A family minted under one of OrcaSlicer's own "OF…" ids (the sync only
        # takes it out of the picker; a spool may still name it): the slot is told
        # Bambu's id for that filament, or — an id the table lacks — the
        # material's generic, said. Every other id is itself.
        printer_fid = catalog.bambu_filament_id(family_id)
        if printer_fid is None:
            stand_in, fam_type = _stand_in(fam_type or material_override or "")
            if stand_in is not None:
                warnings.append(f"{family_id} is no id a printer knows; using {stand_in.filament_id}")
                degraded = "family_unresolved"
                family_id = stand_in.filament_id
        else:
            family_id = printer_fid

    # support_user_preset gate (spec A §5.2): P* only when the device says so.
    # The same rule as above: the id is replaced, the family's type stays.
    if family_id.startswith("P") and not supports_user_preset:
        stand_in, fam_type = _stand_in(fam_type or material_override or "")
        if stand_in is not None:
            warnings.append(f"printer does not support user presets; degraded {family_id} -> {stand_in.filament_id}")
            degraded = "user_preset_unsupported"
            family_id = stand_in.filament_id

    candidates = _candidate_printer_names(printer_model, nozzle_diameter)
    preset = catalog.preset_for_setting_id(preset_setting_id) if preset_setting_id else None
    if preset is None:
        preset = _pick_preset(family_id, candidates)

    # A family the user created has no catalogue presets, so the pick above
    # finds nothing and the slot went out with no preset and 200–240 °C. Its
    # presets are the user's own, one per printer it was made for (audit D2).
    # Only where the printer takes user presets — the gate above has already
    # swapped a P family for a system one on any other.
    user_preset = temps_preset = None
    if supports_user_preset and not preset_setting_id and catalog.get_family(family_id) is None:
        user_preset, temps_preset = _user_presets_for_printer(
            await family_user_presets(db, family_id), printer_model, nozzle_diameter
        )

    # setting_id precedence: an explicit request > the user's own preset made
    # for this printer > the identity's own (a mirrored cloud preset keeps its
    # PFUS/uuid — the #1815 guarantee) > the catalog preset picked for this
    # printer.
    setting_id = (
        (preset_setting_id or None)
        or (bambu_preset_id(user_preset) if user_preset is not None else None)
        or (resolved.setting_id if resolved else None)
        or (preset.setting_id if preset else None)
        or ""
    )
    temp_min = (
        temp_overrides[0]
        or (user_preset.nozzle_temp_min if user_preset is not None else None)
        or (preset.nozzle_temp_min if preset else None)
        or (resolved.nozzle_temp_min if resolved else None)
        or (temps_preset.nozzle_temp_min if temps_preset is not None else None)
        or 200
    )
    temp_max = (
        temp_overrides[1]
        or (user_preset.nozzle_temp_max if user_preset is not None else None)
        or (preset.nozzle_temp_max if preset else None)
        or (resolved.nozzle_temp_max if resolved else None)
        or (temps_preset.nozzle_temp_max if temps_preset is not None else None)
        or 240
    )

    cols: list[str] = []
    ctype = 0
    stops = [c.strip() for c in (extra_colors or "").split(",") if c.strip()]
    if stops:
        cols = [color_rgba] + [c if len(c) == 8 else c + "FF" for c in stops]
        ctype = 1

    for note in warnings:
        logger.info("slot assignment: %s", note)
    return SlotAssignmentPlan(
        tray_info_idx=family_id,
        setting_id=setting_id,
        tray_type=fam_type or (material_override or ""),
        tray_color=color_rgba,
        cols=cols,
        ctype=ctype,
        nozzle_temp_min=int(temp_min),
        nozzle_temp_max=int(temp_max),
        warnings=warnings,
        degraded=degraded,
        from_stored_preset=from_stored_preset,
    )
