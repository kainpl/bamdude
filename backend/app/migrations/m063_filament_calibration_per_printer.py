"""Move filament_calibration scope from printer_model to printer_id.

Reason: K-profiles are per-printer-instance, not per-model. Two X1Cs in the
same farm can need different K values for the same material due to hardware
wear / belt tension / nozzle age. Per user mental model:
"к-профілі калібруються для кожного принтера окремо".

What this migration does:
    1. ADD COLUMN filament_calibration.printer_id INTEGER REFERENCES printers(id)
       ON DELETE CASCADE (was previously only printer_model + optional
       calibrated_on_printer_id).
    2. Backfill: printer_id = calibrated_on_printer_id (m062 set this from
       the wizard's session.printer_id).
    3. Drop orphans (printer_id IS NULL after backfill — impossible to know
       which printer they're for). One-time log warning.
    4. Drop the OLD partial unique index on (printer_model, ...).
    5. Create NEW partial unique on (printer_id, ...).
    6. Drop lookup index on printer_model; create new on printer_id.
    7. DROP COLUMN printer_model.

Postgres + SQLite (3.35+) both support DROP COLUMN natively.
"""

import logging

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import column_exists, table_exists

logger = logging.getLogger(__name__)

version = 63
name = "filament_calibration_per_printer"


async def upgrade(conn) -> None:
    if not await table_exists(conn, "filament_calibration"):
        return  # Fresh install; nothing to migrate

    # 1. Add printer_id column if absent
    if not await column_exists(conn, "filament_calibration", "printer_id"):
        if is_postgres():
            await conn.execute(
                text(
                    "ALTER TABLE filament_calibration ADD COLUMN printer_id INTEGER "
                    "REFERENCES printers(id) ON DELETE CASCADE"
                )
            )
        else:
            # SQLite: ALTER TABLE ADD COLUMN doesn't support inline FK; we
            # add the column and rely on application-level integrity.
            await conn.execute(text("ALTER TABLE filament_calibration ADD COLUMN printer_id INTEGER"))

    # 2. Backfill from calibrated_on_printer_id
    await conn.execute(
        text(
            "UPDATE filament_calibration "
            "SET printer_id = calibrated_on_printer_id "
            "WHERE printer_id IS NULL AND calibrated_on_printer_id IS NOT NULL"
        )
    )

    # 3. Drop orphans (no printer_id resolvable)
    orphan_count = (
        await conn.execute(text("SELECT COUNT(*) FROM filament_calibration WHERE printer_id IS NULL"))
    ).scalar() or 0
    if orphan_count:
        logger.warning(
            "m063: dropping %d filament_calibration rows with no resolvable "
            "printer_id (calibrated_on_printer_id was NULL — likely seed/dev rows).",
            orphan_count,
        )
        await conn.execute(text("DELETE FROM filament_calibration WHERE printer_id IS NULL"))

    # 4. Drop OLD partial unique index
    await conn.execute(text("DROP INDEX IF EXISTS ux_filament_cali_active"))

    # 5. Drop OLD lookup index
    await conn.execute(text("DROP INDEX IF EXISTS ix_filament_cali_lookup"))

    # 6. Create NEW partial unique on printer_id
    if is_postgres():
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_filament_cali_active "
                "ON filament_calibration "
                "(printer_id, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id) "
                "WHERE is_active = TRUE"
            )
        )
    else:
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_filament_cali_active "
                "ON filament_calibration "
                "(printer_id, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id) "
                "WHERE is_active = 1"
            )
        )

    # 7. Create new lookup index
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_filament_cali_lookup "
            "ON filament_calibration "
            "(printer_id, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id)"
        )
    )

    # 8. Drop printer_model column
    if await column_exists(conn, "filament_calibration", "printer_model"):
        await conn.execute(text("ALTER TABLE filament_calibration DROP COLUMN printer_model"))
