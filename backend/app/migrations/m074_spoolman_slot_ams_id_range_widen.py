"""Widen ``ck_spoolman_slot_ams_id_range`` to admit AMS-HT (ams_id 128–191).

H2C / H2D AMS-HT units report ``ams_id`` in the ``128..191`` range (one
``ams_id`` per AMS-HT unit, single tray per unit). The original
``m053`` constraint admitted only ``0..7 OR 255``, so every attempt to
link a Spoolman spool to an AMS-HT slot died at the DB layer with::

    CHECK constraint failed: ck_spoolman_slot_ams_id_range

The internal ``spool_assignment`` table has no equivalent constraint and
worked fine — this fixes the Spoolman branch to parity.

PostgreSQL: ``DROP CONSTRAINT IF EXISTS`` + ``ADD CONSTRAINT`` (atomic).

SQLite: detect the stale formula in ``sqlite_master.sql`` and rebuild
the table via the ``_v2 → drop → rename`` pattern. Row count is
re-verified after copy; failure aborts the nested transaction so we
never end up with a partial copy. Fresh installs already get the
widened constraint via the model + ``Base.metadata.create_all()`` — the
SQLite rebuild path becomes a no-op there because the formula is
already updated.

Upstream Bambuddy #1274 / commit ``af52c4f2``.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres

version = 74
name = "spoolman_slot_ams_id_range_widen"

_CONSTRAINT_NAME = "ck_spoolman_slot_ams_id_range"
_NEW_FORMULA = "(ams_id >= 0 AND ams_id <= 7) OR (ams_id >= 128 AND ams_id <= 191) OR ams_id = 255"


async def upgrade(conn):
    if is_postgres():
        await conn.execute(text(f"ALTER TABLE spoolman_slot_assignments DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}"))
        await conn.execute(
            text(f"ALTER TABLE spoolman_slot_assignments ADD CONSTRAINT {_CONSTRAINT_NAME} CHECK ({_NEW_FORMULA})")
        )
        return

    row = (
        await conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='spoolman_slot_assignments'")
        )
    ).fetchone()
    if not row:
        return
    sql = row[0] or ""
    # Already widened (fresh install via create_all, or an earlier rerun).
    if "ams_id >= 128" in sql:
        return
    # If neither the constraint name nor the old narrow predicate is present,
    # the table predates the named CHECK constraint — don't risk a rebuild
    # for a constraint that isn't blocking anyone; the model-level guard and
    # app-level validation continue to hold.
    if _CONSTRAINT_NAME not in sql and "ams_id <= 7" not in sql:
        return

    async with conn.begin_nested():
        await conn.execute(text("DROP TABLE IF EXISTS spoolman_slot_assignments_v2"))
        await conn.execute(
            text(
                "CREATE TABLE spoolman_slot_assignments_v2 ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE, "
                f"ams_id INTEGER NOT NULL CONSTRAINT {_CONSTRAINT_NAME} CHECK ({_NEW_FORMULA}), "
                "tray_id INTEGER NOT NULL CONSTRAINT ck_spoolman_slot_tray_id_range "
                "CHECK (tray_id >= 0 AND tray_id <= 3), "
                "spoolman_spool_id INTEGER NOT NULL, "
                "assigned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "CONSTRAINT uq_spoolman_slot_assignment UNIQUE (printer_id, ams_id, tray_id)"
                ")"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO spoolman_slot_assignments_v2 "
                "(id, printer_id, ams_id, tray_id, spoolman_spool_id, assigned_at) "
                "SELECT id, printer_id, ams_id, tray_id, spoolman_spool_id, assigned_at "
                "FROM spoolman_slot_assignments"
            )
        )
        original = (await conn.execute(text("SELECT count(*) FROM spoolman_slot_assignments"))).scalar_one()
        copied = (await conn.execute(text("SELECT count(*) FROM spoolman_slot_assignments_v2"))).scalar_one()
        if copied != original:
            raise RuntimeError(
                f"spoolman_slot_assignments widen: row count mismatch after copy "
                f"({original} in source, {copied} in copy)"
            )
        await conn.execute(text("DROP TABLE spoolman_slot_assignments"))
        await conn.execute(text("ALTER TABLE spoolman_slot_assignments_v2 RENAME TO spoolman_slot_assignments"))
        # m053 doesn't declare any standalone indexes on this table — the
        # ``UNIQUE`` and ``CHECK`` constraints survive the rebuild via the
        # ``CREATE TABLE`` above, and the implicit PK + UNIQUE indexes are
        # recreated automatically by SQLite as part of the new table. So
        # nothing else to do.
