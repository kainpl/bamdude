"""The ONE builder of ams_filament_setting payloads (spec A §5.2). Both
routes (spool assign, manual slot configure) feed a family in and publish
what comes out. BS payload parity: tray_info_idx = family id, versioned
setting_id from the catalog, temps from the preset for THIS printer,
cols/ctype for multi-colour, NO tray_sub_brands. Custom (P*) families are
gated on the device's support_user_preset flag and degrade to the generic
family of the same type, loudly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.services.filament_identity import resolve_spool, resolve_tray
from backend.app.utils import filament_catalog as catalog
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


def _pick_preset(family_id: str, candidates: list[str]) -> catalog.CatalogPreset | None:
    presets = catalog.presets_for_family(family_id)
    for preset in presets:
        if any(name in preset.compatible_printers for name in candidates):
            return preset
    return presets[0] if presets else None


async def build_slot_assignment(
    db: AsyncSession,
    *,
    spool=None,
    family_id: str | None = None,
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
    fam_type = (resolved.filament_type if resolved and resolved.family else None) or (material_override or "")

    # No family (or unknown) -> generic of the material, warning-logged.
    if not resolved or not resolved.family:
        generic = catalog.generic_family_for_material(material_override or "")
        if generic is None:
            raise ValueError(f"no filament family resolvable (family_id={family_id!r}, material={material_override!r})")
        warnings.append(f"family unresolved; using generic {generic.filament_id}")
        family_id = generic.filament_id
        fam_type = generic.filament_type or fam_type
    else:
        family_id = resolved.family.filament_id

    # support_user_preset gate (spec A §5.2): P* only when the device says so.
    if family_id.startswith("P") and not supports_user_preset:
        generic = catalog.generic_family_for_material(fam_type or material_override or "")
        if generic is not None:
            warnings.append(f"printer does not support user presets; degraded {family_id} -> {generic.filament_id}")
            family_id = generic.filament_id
            fam_type = generic.filament_type or fam_type

    candidates = _candidate_printer_names(printer_model, nozzle_diameter)
    preset = catalog.preset_for_setting_id(preset_setting_id) if preset_setting_id else None
    if preset is None:
        preset = _pick_preset(family_id, candidates)

    # setting_id precedence: an explicit request > the identity's own (a
    # mirrored cloud preset keeps its PFUS/uuid — the #1815 guarantee) > the
    # catalog preset picked for this printer.
    setting_id = (
        (preset_setting_id or None)
        or (resolved.setting_id if resolved else None)
        or (preset.setting_id if preset else None)
        or ""
    )
    temp_min = (
        temp_overrides[0]
        or (preset.nozzle_temp_min if preset else None)
        or (resolved.nozzle_temp_min if resolved else None)
        or 200
    )
    temp_max = (
        temp_overrides[1]
        or (preset.nozzle_temp_max if preset else None)
        or (resolved.nozzle_temp_max if resolved else None)
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
    )
