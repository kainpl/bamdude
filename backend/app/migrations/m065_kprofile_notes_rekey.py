"""Re-key ``kprofile_notes`` from ``(printer_id, setting_id)`` to ``filament_calibration_id``.

Background: the old keying assumed ``setting_id`` was stable, but BamDude
observed it changing across restarts in production — notes drifted to wrong
profiles or disappeared. The new keying uses our own stable PK
(``filament_calibration.id``) so notes survive printer restarts, re-syncs
and reorders.

User-confirmed clean-slate: existing notes are dropped (they were already
unreliable; partial recovery via stale ``setting_id`` is not worth the code
complexity).

Idempotent: guarded by ``column_exists`` / ``table_exists``.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import column_exists, table_exists

version = 65
name = "kprofile_notes_rekey"


async def upgrade(conn) -> None:
    if not await table_exists(conn, "kprofile_notes"):
        return

    # Already migrated?
    if await column_exists(conn, "kprofile_notes", "filament_calibration_id") and not await column_exists(
        conn, "kprofile_notes", "setting_id"
    ):
        return

    # 1. Drop all rows (clean slate; setting_id was unstable so partial
    # recovery would land notes on wrong profiles).
    await conn.execute(text("DELETE FROM kprofile_notes"))

    # 2. Drop OLD indexes (names from m000 import + later migrations).
    await conn.execute(text("DROP INDEX IF EXISTS ix_kprofile_notes_printer_setting"))
    await conn.execute(text("DROP INDEX IF EXISTS uq_kprofile_notes_printer_setting"))

    # 3. Add filament_calibration_id column.
    if not await column_exists(conn, "kprofile_notes", "filament_calibration_id"):
        if is_postgres():
            await conn.execute(
                text(
                    "ALTER TABLE kprofile_notes ADD COLUMN filament_calibration_id INTEGER "
                    "REFERENCES filament_calibration(id) ON DELETE CASCADE NOT NULL"
                )
            )
        else:
            await conn.execute(text("ALTER TABLE kprofile_notes ADD COLUMN filament_calibration_id INTEGER NOT NULL"))

    # 4. Drop OLD columns (printer_id, setting_id).
    for col in ("setting_id", "printer_id"):
        if await column_exists(conn, "kprofile_notes", col):
            await conn.execute(text(f"ALTER TABLE kprofile_notes DROP COLUMN {col}"))

    # 5. Create new unique index on the FK.
    await conn.execute(
        text("CREATE UNIQUE INDEX IF NOT EXISTS uq_kprofile_notes_fc ON kprofile_notes (filament_calibration_id)")
    )
