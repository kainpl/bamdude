"""Move ``filament_calibration`` scope from ``printer_model`` to ``printer_id``;
drop redundant ``calibrated_on_printer_id``.

Reason: K-profiles are per-printer-instance, not per-model. Two X1Cs in the
same farm can need different K values for the same material due to hardware
wear / belt tension / nozzle age. Per user mental model:
"к-профілі калібруються для кожного принтера окремо". After the move
``calibrated_on_printer_id`` is redundant — it always equals ``printer_id``
since calibrations don't get copied across instances — so we drop it too.

What this migration does:
    1. ADD COLUMN ``filament_calibration.printer_id`` (FK on Postgres,
       application-level integrity on SQLite — ALTER TABLE ADD COLUMN
       there refuses inline FK).
    2. Backfill ``printer_id = calibrated_on_printer_id`` for any row that
       still has it (m062 wrote it from the wizard's session.printer_id).
    3. Drop orphans where ``printer_id`` couldn't be resolved.
    4-7. Replace the partial-unique + lookup indexes so they key on
       ``printer_id`` instead of ``printer_model``.
    8. Drop the OLD ``printer_model`` and ``calibrated_on_printer_id``
       columns. SQLite's ``ALTER TABLE DROP COLUMN`` refuses to drop a
       column that's still referenced by a FOREIGN KEY clause in the
       table's own schema (``calibrated_on_printer_id`` is one) — rebuild
       via ``recreate_table`` instead. Postgres handles both columns with
       a plain ``DROP COLUMN`` since the FK is on the dropped column
       itself and gets cleaned up alongside it.
"""

import logging

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import column_exists, recreate_table, table_exists

logger = logging.getLogger(__name__)

version = 63
name = "filament_calibration_per_printer"


# Target shape after m063. Matches the model + what fresh installs would get
# from a future from-scratch CREATE TABLE.
_POST_M063_DDL = """CREATE TABLE filament_calibration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
    filament_id TEXT NOT NULL,
    filament_setting_id TEXT,
    nozzle_diameter REAL NOT NULL,
    nozzle_volume_type TEXT NOT NULL,
    extruder_id INTEGER NOT NULL DEFAULT 0,
    pa_k_value REAL,
    pa_n_coef REAL,
    flow_ratio REAL,
    confidence INTEGER,
    cali_mode TEXT NOT NULL,
    source TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    cali_idx INTEGER,
    name TEXT NOT NULL,
    notes TEXT,
    nozzle_id TEXT,
    calibrated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)"""

_COLUMNS_TO_COPY = (
    "id, printer_id, filament_id, filament_setting_id, nozzle_diameter, "
    "nozzle_volume_type, extruder_id, pa_k_value, pa_n_coef, flow_ratio, "
    "confidence, cali_mode, source, is_active, cali_idx, name, notes, nozzle_id, "
    "calibrated_by_user_id, created_at"
)


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
            # add the column and reattach the FK at the recreate step below.
            await conn.execute(text("ALTER TABLE filament_calibration ADD COLUMN printer_id INTEGER"))

    # 1b. Add nozzle_id column if absent (added late — older m062 schemas
    # didn't carry it). Fresh installs from the new m062 already have it;
    # older installs get it here so the rebuild copy list below is valid.
    if not await column_exists(conn, "filament_calibration", "nozzle_id"):
        if is_postgres():
            await conn.execute(text("ALTER TABLE filament_calibration ADD COLUMN nozzle_id VARCHAR(20)"))
        else:
            await conn.execute(text("ALTER TABLE filament_calibration ADD COLUMN nozzle_id TEXT"))

    # 2. Backfill from calibrated_on_printer_id (only meaningful when the
    # legacy column still exists — guard for fresh installs and re-runs).
    if await column_exists(conn, "filament_calibration", "calibrated_on_printer_id"):
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

    # 6. Drop OLD columns. SQLite needs the table rebuild because
    # ``calibrated_on_printer_id`` carries an inline FK to ``printers(id)``
    # and ``ALTER TABLE DROP COLUMN`` refuses any column referenced by a
    # FOREIGN KEY clause in the table's own schema. Postgres can drop both
    # columns straight — the FK on ``calibrated_on_printer_id`` is on the
    # column itself and gets cleaned up alongside it.
    before = (await conn.execute(text("SELECT COUNT(*) FROM filament_calibration"))).scalar() or 0
    if is_postgres():
        for col in ("printer_model", "calibrated_on_printer_id"):
            if await column_exists(conn, "filament_calibration", col):
                await conn.execute(text(f"ALTER TABLE filament_calibration DROP COLUMN IF EXISTS {col}"))
    else:
        await recreate_table(conn, "filament_calibration", _POST_M063_DDL, _COLUMNS_TO_COPY)
    after = (await conn.execute(text("SELECT COUNT(*) FROM filament_calibration"))).scalar() or 0
    if after != before:
        raise RuntimeError(f"m063 recreate lost rows: before={before} after={after}")

    # 7. Re-create the new indexes on the (rebuilt) table.
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
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_filament_cali_lookup "
            "ON filament_calibration "
            "(printer_id, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id)"
        )
    )
