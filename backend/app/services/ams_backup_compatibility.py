"""AMS Backup compatibility emulation — the ONE place the advertised profile is decided.

Spec: vault 60-specs/ams-backup-compatibility-emulation-spec-plan. The firmware
groups auto-refill spools by preset + colour (+ RFID identity). This module
takes the ACTUAL SlotAssignmentPlan of a spool and returns what we ADVERTISE to
the printer instead: a canonical colour and/or the Generic family of the base
material. The actual plan is never mutated; routing, dispatch and accounting
keep reading the real spool through services/ams_advertised_overlay.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal

from backend.app.services.slot_assignment import SlotAssignmentPlan, build_slot_assignment
from backend.app.services.spool_tag_matcher import is_valid_tag
from backend.app.utils import filament_catalog as catalog

NAMESPACE = "backup_compatibility"
DEFAULT_CANONICAL_COLOR = "000000FF"
_OPAQUE_RGBA = re.compile(r"[0-9A-F]{6}FF")

# ⚠️ HARDWARE-DECIDED KNOBS (spec §8). Change only with a capture on P1S+AMS
# and X2D+AMS 2 Pro recorded in the vault. Nothing else in the codebase may
# hardcode which materials are "base" or whether K follows a generic tray.
GENERIC_BASE_MATERIALS: frozenset[str] = frozenset({"PLA", "PETG", "ABS", "ASA", "TPU"})
GENERIC_MODE_KPROFILE: Literal["actual", "skip"] = "actual"

REASON_POLICY_OFF = "policy_off"
REASON_RFID = "rfid_slot_excluded"
REASON_EXTERNAL = "external_slot_excluded"
REASON_BASE_MATERIAL = "base_material_not_allowed"
REASON_GENERIC_PRESET = "generic_preset_unavailable"
# Bulk-apply only (services/ams_backup_compatibility_apply.py):
REASON_PRINTER_BUSY = "printer_busy"
REASON_SLOT_EMPTY = "slot_empty"
REASONS = frozenset(
    {
        REASON_POLICY_OFF,
        REASON_RFID,
        REASON_EXTERNAL,
        REASON_BASE_MATERIAL,
        REASON_GENERIC_PRESET,
        REASON_PRINTER_BUSY,
        REASON_SLOT_EMPTY,
    }
)

APPLIED_GENERIC = "generic"
APPLIED_COLOR = "color"


@dataclass(frozen=True)
class BackupCompatibilityPolicy:
    """What a printer was told to advertise — the consumed form of the
    ``backup_compatibility`` namespace.

    Deliberately a second class beside ``schemas/printer.BackupCompatibilityPolicy``:
    the schema validates the wire (and REFUSES a bad colour), this one is read
    off a persisted row that may predate any validator, so it CORRECTS instead.
    """

    normalize_color: bool = False
    canonical_color_rgba: str = DEFAULT_CANONICAL_COLOR
    generic_base_material: bool = False

    @property
    def enabled(self) -> bool:
        return self.normalize_color or self.generic_base_material

    @classmethod
    def from_dict(cls, raw: dict | None) -> BackupCompatibilityPolicy:
        raw = raw or {}
        color = str(raw.get("canonical_color_rgba") or DEFAULT_CANONICAL_COLOR).strip().lstrip("#").upper()
        if not _OPAQUE_RGBA.fullmatch(color):
            color = DEFAULT_CANONICAL_COLOR
        return cls(
            normalize_color=bool(raw.get("normalize_color", False)),
            canonical_color_rgba=color,
            generic_base_material=bool(raw.get("generic_base_material", False)),
        )

    @classmethod
    def from_printer(cls, printer) -> BackupCompatibilityPolicy:
        policies = getattr(printer, "ams_policies", None) or {}
        return cls.from_dict(policies.get(NAMESPACE) if isinstance(policies, dict) else None)


@dataclass(frozen=True)
class SlotProjection:
    """Both halves of one slot: what the spool IS and what the printer is told.

    ``reasons`` explains a projection that did NOT happen — it is why the
    advertised plan is the actual one, never a complaint about an applied mode.
    """

    actual: SlotAssignmentPlan
    advertised: SlotAssignmentPlan
    applied: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def projected(self) -> bool:
        return bool(self.applied)


def slot_is_rfid(live_tray: dict | None, spool_tag_uid: str | None, spool_tray_uuid: str | None) -> bool:
    """RFID identity is authoritative for the firmware — never masked.

    A stale ``tag_uid`` the printer keeps after a Bambu spool was removed makes
    this answer True for a manual spool; that errs on the safe side (no
    projection, today's payload)."""
    if live_tray and is_valid_tag(str(live_tray.get("tag_uid") or ""), str(live_tray.get("tray_uuid") or "")):
        return True
    return is_valid_tag(spool_tag_uid or "", spool_tray_uuid or "")


def generic_family_for_base_material(material: str) -> catalog.CatalogFamily | None:
    """Generic family ONLY for an exact base material: ``PETG-CF`` is not PETG
    here, although ``catalog.generic_family_for_material`` would split it so."""
    mat = (material or "").strip().upper()
    if mat not in GENERIC_BASE_MATERIALS:
        return None
    family = catalog.generic_family_for_material(mat)
    if family is None or (family.filament_type or "").strip().upper() != mat:
        return None
    return family


def live_tray_for(state, ams_id: int, tray_id: int) -> dict | None:
    """The live AMS tray dict from a PrinterState (external slots: None — excluded anyway).

    Not ``routes/inventory._find_tray_in_ams_data``: that one takes an already
    unwrapped list and matches on ``id`` alone, while a single-slot unit
    (AMS HT, id >= 128) reports its one tray under an id that need not be the
    slot we were asked for."""
    raw = getattr(state, "raw_data", None) or {}
    units = raw.get("ams", [])
    if isinstance(units, dict):
        units = units.get("ams", [])
    for unit in units or []:
        if str(unit.get("id")) != str(ams_id):
            continue
        for tray in unit.get("tray", []) or []:
            if str(tray.get("id")) == str(tray_id):
                return tray
        trays = unit.get("tray", []) or []
        return trays[0] if ams_id >= 128 and len(trays) == 1 else None
    return None


def kprofile_allowed(projection) -> bool:
    """Whether the K-profile push may follow a projection (spec §6.5)."""
    return not (APPLIED_GENERIC in projection.applied and GENERIC_MODE_KPROFILE == "skip")


async def project_slot_assignment(
    db,
    *,
    actual: SlotAssignmentPlan,
    policy: BackupCompatibilityPolicy,
    live_tray: dict | None,
    spool_tag_uid: str | None,
    spool_tray_uuid: str | None,
    ams_id: int,
    material: str | None,
    extra_colors: str | None,
    printer_model: str | None,
    nozzle_diameter: str,
    supports_user_preset: bool,
) -> SlotProjection:
    """Pure with respect to MQTT and DB writes; the only await is the catalog builder."""
    if not policy.enabled:
        return SlotProjection(actual, actual, reasons=(REASON_POLICY_OFF,))
    if ams_id in (254, 255):
        return SlotProjection(actual, actual, reasons=(REASON_EXTERNAL,))
    if slot_is_rfid(live_tray, spool_tag_uid, spool_tray_uuid):
        return SlotProjection(actual, actual, reasons=(REASON_RFID,))

    advertised = actual
    applied: list[str] = []
    reasons: list[str] = []

    if policy.generic_base_material:
        generic = generic_family_for_base_material(material or actual.tray_type)
        if generic is None:
            reasons.append(REASON_BASE_MATERIAL)
        else:
            # The gradient comes off the ACTUAL plan, not off the caller. The
            # builder writes cols as ``[base] + stops``, so the stops are
            # already there; asking a caller to hand them over a second time is
            # a two-arguments-in-sync footgun that fails silently — the rebuilt
            # plan would come back flat (cols=[], ctype=0) and the printer would
            # simply be told a one-colour tray, with colour mode off.
            if extra_colors is None and len(actual.cols) > 1:
                extra_colors = ",".join(actual.cols[1:])
            try:
                # A whole rebuild, not a field swap: the generic family carries
                # its OWN setting_id and per-printer temps, and a tray whose
                # preset and temps disagree is exactly what the firmware
                # refuses to group.
                rebuilt = await build_slot_assignment(
                    db,
                    family_id=generic.filament_id,
                    material_override=generic.filament_type or material,
                    color_rgba=actual.tray_color,
                    extra_colors=extra_colors,
                    printer_model=printer_model,
                    nozzle_diameter=nozzle_diameter,
                    supports_user_preset=supports_user_preset,
                )
            except ValueError:
                rebuilt = None
            if rebuilt is None or not rebuilt.setting_id:
                reasons.append(REASON_GENERIC_PRESET)
            else:
                advertised = rebuilt
                applied.append(APPLIED_GENERIC)

    if policy.normalize_color:
        # cols/ctype go too: a multi-colour tray the firmware sees as gradient
        # can never match a flat canonical colour, so leaving them would make
        # the colour mode a no-op on exactly the spools that need it.
        advertised = replace(advertised, tray_color=policy.canonical_color_rgba, cols=[], ctype=0)
        applied.append(APPLIED_COLOR)

    return SlotProjection(actual, advertised, tuple(applied), tuple(reasons))
