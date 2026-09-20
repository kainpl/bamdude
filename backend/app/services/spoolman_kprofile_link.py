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

# A slot's nozzle and a stored profile's nozzle are floats read from two
# different sources; 0.4 vs 0.4000001 is the same nozzle.
NOZZLE_TOLERANCE = 0.05


async def resolve_spoolman_slot_kprofile(
    db,
    *,
    printer_id: int,
    spoolman_spool_id: int,
    nozzle_diameter: float,
    slot_extruder: int | None,
) -> FilamentCalibration | None:
    """The calibration linked to this Spoolman spool that fits this slot, if any.

    ``slot_extruder`` is None when the printer reports no extruder map (every
    single-extruder machine): the link still applies, it just cannot be
    preferred over another one.
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
    exact: FilamentCalibration | None = None
    fallback: FilamentCalibration | None = None
    for kp in rows:
        fc = kp.filament_calibration
        if not fc or abs(fc.nozzle_diameter - nozzle_diameter) > NOZZLE_TOLERANCE:
            continue
        if slot_extruder is not None and kp.extruder == slot_extruder:
            exact = fc
            break
        if fallback is None:
            fallback = fc
    return exact or fallback
