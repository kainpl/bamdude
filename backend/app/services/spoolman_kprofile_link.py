"""The ONE resolver of «which stored K-profile belongs in this AMS slot».

Both Spoolman assign paths (``routes/spoolman_inventory.assign_spoolman_slot``
and ``routes/spoolman.link_spool``) and the backup-compatibility walker
(``services/ams_backup_compatibility_apply``) ask this, so the family a
Spoolman slot is built from — the BRANDED family of the linked calibration,
never the generic of the material — is decided in one place. A second copy of
the pick is how a bulk revert came to publish a plan the slot never had, and
how the K re-push came to carry the generic id the routes deliberately avoid.
"""

from __future__ import annotations

from sqlalchemy import select

from backend.app.models.filament_calibration import FilamentCalibration
from backend.app.models.spoolman_k_profile import SpoolmanKProfile
from backend.app.services.spoolman_spool_identity import OTHER, verdict

# A slot's nozzle and a stored profile's nozzle are floats read from two
# different sources; 0.4 vs 0.4000001 is the same nozzle.
NOZZLE_TOLERANCE = 0.05


async def resolve_spoolman_slot_kprofile(
    db,
    *,
    printer_id: int,
    spoolman_spool_id: int,
    spool_registered: str | None,
    nozzle_diameter: float,
    slot_extruder: int | None,
    nozzle_flow: str | None = None,
) -> FilamentCalibration | None:
    """The calibration linked to this Spoolman spool that fits this slot, if any.

    ``spool_registered`` is the stamp of the spool the caller proved — no default: every
    caller says which spool it means (spec ``spoolman-binding-identity`` §5.4). A link
    stamped for another spool, an earlier holder of the number, is not this spool's; an
    unstamped link (before m266) still applies. A read: nothing is written here.

    ``slot_extruder`` is None when the printer reports no extruder map (every
    single-extruder machine): the link still applies, it just cannot be
    preferred over another one.

    ``nozzle_flow`` is the flow type of the nozzle the slot feeds (see
    ``utils/slot_nozzle``). A spool may hold a Standard AND a High Flow link for
    one hotend and diameter — BambuStudio's identity for a PA profile — and the
    fitted nozzle picks between them. It is a preference, not a filter: a spool
    linked for one flow type only, or a printer that reports no flow class,
    still gets its profile.
    """
    rows = (
        (
            await db.execute(
                select(SpoolmanKProfile).where(
                    SpoolmanKProfile.spoolman_spool_id == spoolman_spool_id,
                    SpoolmanKProfile.printer_id == printer_id,
                )
            )
        )
        .scalars()
        .all()
    )
    candidates = [
        (kp, kp.filament_calibration)
        for kp in rows
        if kp.filament_calibration
        and verdict(kp.spool_registered, {"registered": spool_registered}) != OTHER
        and abs(kp.filament_calibration.nozzle_diameter - nozzle_diameter) <= NOZZLE_TOLERANCE
    ]

    def rank(pair) -> tuple[bool, bool]:
        kp, fc = pair
        own_extruder = slot_extruder is not None and kp.extruder == slot_extruder
        own_flow = bool(nozzle_flow) and (fc.nozzle_volume_type or "standard") == nozzle_flow
        return own_extruder, own_flow

    # The slot's own hotend first, then its fitted flow; ties keep the stored
    # order, which is what the first-match loop this replaces answered.
    return max(candidates, key=rank)[1] if candidates else None
