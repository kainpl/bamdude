"""A rack AMS and a direct rack's external inputs in ``spoolman_slot_assignments``.

H3C and P3C (N01 stage A) have a direct-connected nozzle rack: an AMS 3H
(unit 32, nine slots) or an AMS 3P (unit 33, seven), and an external input per
PrintTool, stored as ``(255, 0..8)`` (254 -> 0, 255 -> 1, 247..253 -> 2..8,
``utils/tray_ids``). The CHECKs m053 wrote and m074 widened admit units 0-7,
128-191 and 255 with trays 0-3, so a Spoolman spool bound to slot 6 of an
AMS 3H, or to PrintTool 9's input, died at the INSERT.

New form, names kept:

* ``ck_spoolman_slot_ams_id_range`` adds 32 and 33;
* ``ck_spoolman_slot_tray_id_range`` is ``tray_id >= 0 AND (tray_id <= 3 OR
  (ams_id IN (32, 33, 255) AND tray_id <= 8))``.

The database holds the widest rack (nine); a P3C's seven is the printer's own
bound, checked in code (``TrayLayout.accepts_slot``). Every old row satisfies
the new CHECKs — they only admit more — so nothing is dropped or rewritten.

PostgreSQL: drop and re-add both constraints. SQLite: rebuild the table with
the current DDL (``recreate_table``, indexes carried); a table already in the
new form (a fresh install built from the model, or a rerun) is left alone.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import recreate_table, table_exists

version = 272
name = "spoolman_slot_rack_trays"

_TABLE = "spoolman_slot_assignments"
_AMS_CHECK = "ck_spoolman_slot_ams_id_range"
_TRAY_CHECK = "ck_spoolman_slot_tray_id_range"
_AMS_FORMULA = (
    "(ams_id >= 0 AND ams_id <= 7) OR ams_id IN (32, 33) OR (ams_id >= 128 AND ams_id <= 191) OR ams_id = 255"
)
_TRAY_FORMULA = "tray_id >= 0 AND (tray_id <= 3 OR (ams_id IN (32, 33, 255) AND tray_id <= 8))"

_SQLITE_DDL = (
    f"CREATE TABLE {_TABLE} ("
    "id INTEGER NOT NULL PRIMARY KEY, "
    "printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE, "
    "ams_id INTEGER NOT NULL, "
    "tray_id INTEGER NOT NULL, "
    "spoolman_spool_id INTEGER NOT NULL, "
    "spool_registered VARCHAR(40), "
    "assigned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
    "CONSTRAINT uq_spoolman_slot_assignment UNIQUE (printer_id, ams_id, tray_id), "
    f"CONSTRAINT {_AMS_CHECK} CHECK ({_AMS_FORMULA}), "
    f"CONSTRAINT {_TRAY_CHECK} CHECK ({_TRAY_FORMULA})"
    ")"
)
_COLUMNS = "id, printer_id, ams_id, tray_id, spoolman_spool_id, spool_registered, assigned_at"


async def upgrade(conn):
    if not await table_exists(conn, _TABLE):
        return
    if is_postgres():
        for constraint, formula in ((_AMS_CHECK, _AMS_FORMULA), (_TRAY_CHECK, _TRAY_FORMULA)):
            await conn.execute(text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {constraint}"))
            await conn.execute(text(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {constraint} CHECK ({formula})"))
        return

    row = (
        await conn.execute(text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :t"), {"t": _TABLE})
    ).fetchone()
    sql = (row[0] if row else "") or ""
    if "ams_id IN (32, 33, 255)" in sql:
        return  # already in the new form
    ddl = _SQLITE_DDL
    if "AUTOINCREMENT" in sql.upper():
        # Keep the id rule the table has (m053 / m074 built it with AUTOINCREMENT).
        ddl = ddl.replace("id INTEGER NOT NULL PRIMARY KEY", "id INTEGER PRIMARY KEY AUTOINCREMENT")
    original = (await conn.execute(text(f"SELECT count(*) FROM {_TABLE}"))).scalar_one()
    await recreate_table(conn, _TABLE, ddl, _COLUMNS)
    copied = (await conn.execute(text(f"SELECT count(*) FROM {_TABLE}"))).scalar_one()
    if copied != original:
        raise RuntimeError(f"{_TABLE} rebuild: {original} rows before, {copied} after")


async def replay(conn):
    """Repeat-safe for DEBUG: the form check above makes a second run a no-op."""
    await upgrade(conn)
