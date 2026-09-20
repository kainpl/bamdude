"""AMS Backup compatibility emulation — the ONE place the advertised profile is decided.

Spec: vault 60-specs/ams-backup-compatibility-emulation-spec-plan. The firmware
groups auto-refill spools by preset + colour (+ RFID identity). This module
takes the ACTUAL SlotAssignmentPlan of a spool and returns what we ADVERTISE to
the printer instead: a canonical colour and/or the Generic family of the base
material. The actual plan is never mutated; routing, dispatch and accounting
keep reading the real spool through services/ams_advertised_overlay.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from backend.app.services.slot_assignment import SlotAssignmentPlan, build_slot_assignment
from backend.app.services.spool_tag_matcher import is_valid_tag
from backend.app.utils import filament_catalog as catalog
from backend.app.utils.rgba import normalize_opaque_rgba

NAMESPACE = "backup_compatibility"
DEFAULT_CANONICAL_COLOR = "000000FF"

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
# Bulk-apply only (services/ams_backup_compatibility_apply.py). There is
# deliberately no "printer is busy" reason: a busy printer is refused by the
# route with a 409 before any slot is walked, so a row could never carry it.
# ⚠️ Every member here has a ``printers.amsCompat.reason.*`` string in BOTH
# locales, and that is enforced from both ends — the closed list is pinned by
# ``tests/unit/services/test_ams_backup_compatibility.py`` and by
# ``frontend/src/__tests__/i18n/amsCompatReasons.test.ts``, so a new reason is
# two red tests until its two strings exist.
REASON_SLOT_EMPTY = "slot_empty"
REASONS = frozenset(
    {
        REASON_POLICY_OFF,
        REASON_RFID,
        REASON_EXTERNAL,
        REASON_BASE_MATERIAL,
        REASON_GENERIC_PRESET,
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
        # A persisted namespace is whatever the column holds: a list, a string
        # or a bare ``true`` all reach here from a hand-edited row or a future
        # writer, and ``.get`` on any of them is an AttributeError inside an
        # MQTT callback. Unreadable means default-off, like absent.
        if not isinstance(raw, dict):
            raw = {}
        # Same predicate the schema REFUSES on (``utils/rgba``); here it corrects.
        color = normalize_opaque_rgba(raw.get("canonical_color_rgba")) or DEFAULT_CANONICAL_COLOR
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

    ``reasons`` is why a MODE did not apply — never a complaint about one that
    did, and never a status of its own: a projection with both a reason and an
    applied mode is normal (generic refused, colour applied). ``.projected`` is
    the one question a caller asks; a non-empty ``reasons`` answers nothing.
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
    projection, today's payload).

    ⚠️ A ``None`` live tray (the printer has not pushed this slot yet, or the
    unit is not reporting) falls through to the SPOOL's own tag fields. That is
    defence in depth, not a proof of "no RFID": the inventory row is the only
    thing we can still ask, and it answers for the spool we believe is there.
    Both halves must say "manual" before anything is masked."""
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
        trays = unit.get("tray", []) or []
        for tray in trays:
            if str(tray.get("id")) == str(tray_id):
                return tray
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
    # Gate order is the spec's (§6.1): the switch, then the two hard
    # exclusions in the order they matter to the operator — an RFID spool is
    # never masked wherever it sits, and only then is the external slot out of
    # MVP scope. A slot that is both reports the RFID reason, which is the one
    # that would still hold after external slots are covered.
    if not policy.enabled:
        return SlotProjection(actual, actual, reasons=(REASON_POLICY_OFF,))
    if slot_is_rfid(live_tray, spool_tag_uid, spool_tray_uuid):
        return SlotProjection(actual, actual, reasons=(REASON_RFID,))
    if ams_id in (254, 255):
        return SlotProjection(actual, actual, reasons=(REASON_EXTERNAL,))

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
        # ``warnings`` is copied, not shared: with generic mode off the
        # advertised plan is ``replace``d straight off the ACTUAL one, and
        # ``SlotAssignmentPlan.warnings`` is a mutable list — a caller
        # appending to the advertised plan's notes would otherwise write into
        # the spool's own plan.
        advertised = replace(
            advertised,
            tray_color=policy.canonical_color_rgba,
            cols=[],
            ctype=0,
            warnings=list(advertised.warnings),
        )
        applied.append(APPLIED_COLOR)

    return SlotProjection(actual, advertised, tuple(applied), tuple(reasons))
