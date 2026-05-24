"""Auto-link K-profiles to spools by filament.

Adds:
  - ``spool_k_profile.auto_linked`` / ``spoolman_k_profile.auto_linked`` — marks
    engine-managed links (services/kprofile_autolink.py) vs manual PA-tab links.
  - ``spool.resolved_filament_id`` — normalized GF-form filament_id used for
    matching (base-resolved for custom presets).

The ``seed()`` performs an OFFLINE backfill of auto-links for spools whose
``slicer_filament`` resolves without the cloud (``GF*`` / ``GFS*``). Custom
``P*`` presets are linked at runtime instead (no cloud calls in migrations) —
see the startup resolver in ``main.py``. Selects/updates are by column name
(never ``select(Model)``) so later schema additions can't break this seed.

Idempotent — ``add_column`` is a no-op when the column already exists, and the
seed only inserts links that don't exist yet.
"""

import logging

from sqlalchemy import text

from backend.app.migrations.helpers import add_column
from backend.app.utils.filament_ids import setting_id_to_filament_id

logger = logging.getLogger(__name__)

version = 79
name = "kprofile_autolink"


async def upgrade(conn):
    await add_column(conn, "spool_k_profile", "auto_linked BOOLEAN NOT NULL DEFAULT 0")
    await add_column(conn, "spoolman_k_profile", "auto_linked BOOLEAN NOT NULL DEFAULT 0")
    await add_column(conn, "spool", "resolved_filament_id VARCHAR(50)")


def _offline_filament_id(slicer_filament: str | None) -> str | None:
    """Resolve a slicer preset to a GF-form filament_id WITHOUT the cloud.

    ``GF*`` is returned as-is; ``GFS*`` has its ``S`` stripped. Custom ``P*``
    presets need the cloud ``base_id`` and are skipped here (return None).
    """
    if not slicer_filament:
        return None
    base = slicer_filament.split("_")[0] if "_" in slicer_filament else slicer_filament
    if base.startswith("P"):
        return None
    if base.startswith("GF"):
        return setting_id_to_filament_id(base)
    return None


async def seed(session_factory):
    """Offline backfill: link existing spools to existing calibrations by
    resolved filament_id, mark links ``auto_linked=1``, and auto-activate the
    chosen calibration when its combo has none active.

    Local spools only via this path (Spoolman cache rows carry their resolved
    id and are linked by the runtime resolver). All reads/writes are by column
    name through Core SQL.
    """
    async with session_factory() as session:
        conn = await session.connection()

        # 1. Resolve + persist resolved_filament_id for offline-resolvable spools.
        spools = (await conn.execute(text("SELECT id, slicer_filament, resolved_filament_id FROM spool"))).all()
        spool_fid: dict[int, str] = {}
        for sid, slicer, resolved in spools:
            fid = resolved or _offline_filament_id(slicer)
            if not fid:
                continue
            spool_fid[sid] = fid
            if not resolved:
                await conn.execute(
                    text("UPDATE spool SET resolved_filament_id = :fid WHERE id = :id"),
                    {"fid": fid, "id": sid},
                )

        if not spool_fid:
            await session.commit()
            return

        # 2. For each spool, find matching calibrations (one per combo,
        #    active-first then newest) and create missing auto links.
        for sid, fid in spool_fid.items():
            cals = (
                await conn.execute(
                    text(
                        "SELECT id, nozzle_diameter, nozzle_volume_type, extruder_id, is_active "
                        "FROM filament_calibration WHERE filament_id = :fid "
                        "ORDER BY is_active DESC, id DESC"
                    ),
                    {"fid": fid},
                )
            ).all()
            # Need printer_id per calibration for the link row.
            seen_combo: set[tuple] = set()
            for cal_id, ndia, nvol, ext, is_active in cals:
                combo = (ndia, nvol, ext)
                if combo in seen_combo:
                    continue
                seen_combo.add(combo)

                printer_id = (
                    await conn.execute(
                        text("SELECT printer_id FROM filament_calibration WHERE id = :id"),
                        {"id": cal_id},
                    )
                ).scalar()

                # Skip if any link (manual or auto) already exists for this
                # (spool, printer, extruder, calibration).
                existing = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM spool_k_profile WHERE spool_id = :sid AND "
                            "printer_id = :pid AND extruder = :ext AND filament_calibration_id = :cid"
                        ),
                        {"sid": sid, "pid": printer_id, "ext": ext, "cid": cal_id},
                    )
                ).first()
                if existing:
                    continue

                await conn.execute(
                    text(
                        "INSERT INTO spool_k_profile "
                        "(spool_id, printer_id, extruder, filament_calibration_id, auto_linked) "
                        "VALUES (:sid, :pid, :ext, :cid, 1)"
                    ),
                    {"sid": sid, "pid": printer_id, "ext": ext, "cid": cal_id},
                )

                # Auto-activate if the combo has no active row yet.
                if not is_active:
                    has_active = (
                        await conn.execute(
                            text(
                                "SELECT 1 FROM filament_calibration WHERE printer_id = :pid AND "
                                "filament_id = :fid AND nozzle_diameter = :ndia AND "
                                "nozzle_volume_type = :nvol AND extruder_id = :ext AND is_active = 1"
                            ),
                            {"pid": printer_id, "fid": fid, "ndia": ndia, "nvol": nvol, "ext": ext},
                        )
                    ).first()
                    if not has_active:
                        await conn.execute(
                            text("UPDATE filament_calibration SET is_active = 1 WHERE id = :id"),
                            {"id": cal_id},
                        )

        await session.commit()
