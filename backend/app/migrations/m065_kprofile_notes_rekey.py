"""Re-key ``kprofile_notes`` from ``(printer_id, setting_id)`` to ``filament_calibration_id``.

Background: the old keying assumed ``setting_id`` was stable, but BamDude
observed it changing across restarts in production — notes drifted to wrong
profiles or disappeared. The new keying uses our own stable PK
(``filament_calibration.id``) so notes survive printer restarts, re-syncs
and reorders.

User-confirmed clean-slate: existing notes are dropped (they were already
unreliable; partial recovery via stale ``setting_id`` is not worth the code
complexity).

SQLite's ``ALTER TABLE DROP COLUMN`` refuses to drop a column referenced by
a FOREIGN KEY clause in the table's own schema (the old ``printer_id``
column carried ``REFERENCES printers(id)``). Since the row population is
zero after the user-accepted clean-slate, the cheapest correct path is to
drop the table entirely and recreate it with the new shape — no temp-table
dance, no row copy. Same path applies on Postgres.

Idempotent: guarded by ``column_exists`` / ``table_exists``.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import column_exists, table_exists

version = 65
name = "kprofile_notes_rekey"


_NEW_DDL_POSTGRES = """CREATE TABLE kprofile_notes (
    id SERIAL PRIMARY KEY,
    filament_calibration_id INTEGER NOT NULL REFERENCES filament_calibration(id) ON DELETE CASCADE,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)"""

_NEW_DDL_SQLITE = """CREATE TABLE kprofile_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filament_calibration_id INTEGER NOT NULL REFERENCES filament_calibration(id) ON DELETE CASCADE,
    note TEXT NOT NULL DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)"""


async def upgrade(conn) -> None:
    if not await table_exists(conn, "kprofile_notes"):
        return

    # Already migrated?
    if await column_exists(conn, "kprofile_notes", "filament_calibration_id") and not await column_exists(
        conn, "kprofile_notes", "setting_id"
    ):
        return

    await conn.execute(text("DROP INDEX IF EXISTS ix_kprofile_notes_printer_setting"))
    await conn.execute(text("DROP INDEX IF EXISTS uq_kprofile_notes_printer_setting"))
    await conn.execute(text("DROP TABLE kprofile_notes"))
    await conn.execute(text(_NEW_DDL_POSTGRES if is_postgres() else _NEW_DDL_SQLITE))
    await conn.execute(
        text("CREATE UNIQUE INDEX IF NOT EXISTS uq_kprofile_notes_fc ON kprofile_notes (filament_calibration_id)")
    )
