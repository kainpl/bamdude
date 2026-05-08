"""Spoolman inventory UI: dedicated tables for slot assignments + K-profiles + spool extensions.

When BamDude is configured against a Spoolman backend (vs. local-DB inventory),
slot assignments and K-profile linkages have to key on Spoolman's spool IDs
rather than local ``spools.id`` rows. Two new tables are introduced to keep
the local-DB inventory shape (``spool_assignments`` + ``spool_kprofiles``)
intact and let the two backends coexist:

* ``spoolman_slot_assignments`` — which Spoolman spool ID lives in a given
  ``(printer_id, ams_id, tray_id)``. The unique constraint enforces one
  spool per slot. ``ams_id`` accepts ``0..7`` for AMS units and ``255``
  for the external feed (firmware convention). The b30a2831 + ef7fd4fa
  Spoolman inventory UI work depends on this table being the source of
  truth — Spoolman's own ``spool.location`` field is left untouched (the
  operator can populate it manually as a free-text label).

* ``spoolman_k_profile`` — pressure-advance / linear-advance / setting_id
  per ``(spoolman_spool_id, printer_id, extruder, nozzle_diameter)``.
  Lets a Spoolman spool carry its calibration across BamDude installs that
  share the same Spoolman backend. ``CHECK extruder IN (0, 1)`` covers
  single-extruder (0) + dual-extruder hardware (H2D etc., 0/1).

Two ``spools`` column extensions roll in on the same migration since the
inventory UI wave needs them on every backend (local + Spoolman):

* ``spools.storage_location VARCHAR(255)`` — free-form storage label
  surfaced as a column on the inventory page ("Drybox 3", "Shelf A4", etc.).
  Mirrors the ``location`` field Spoolman exposes on its own spool entries.

* ``spools.tag_uid`` widened from ``VARCHAR(16)`` to ``VARCHAR(32)`` —
  Bambu RFID UIDs are 16 hex chars, but the b30a2831 NFC-write hardening
  path accepts third-party tags whose UIDs run up to 32 hex chars. SQLite
  ignores VARCHAR length entirely so the widening is a no-op there;
  Postgres needs the explicit ``ALTER COLUMN ... TYPE`` to take effect.

Idempotent: ``CREATE TABLE IF NOT EXISTS``, ``add_column`` no-ops on
re-runs, and the column-widen check skips when the target type already
matches.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column, table_exists

version = 53
name = "spoolman_inventory_tables"


async def upgrade(conn):
    # Table 1: spoolman_slot_assignments
    if not await table_exists(conn, "spoolman_slot_assignments"):
        if is_postgres():
            await conn.execute(
                text(
                    """
                    CREATE TABLE spoolman_slot_assignments (
                        id SERIAL PRIMARY KEY,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        ams_id INTEGER NOT NULL,
                        tray_id INTEGER NOT NULL,
                        spoolman_spool_id INTEGER NOT NULL,
                        assigned_at TIMESTAMP NOT NULL DEFAULT now(),
                        CONSTRAINT uq_spoolman_slot_assignment UNIQUE (printer_id, ams_id, tray_id),
                        CONSTRAINT ck_spoolman_slot_ams_id_range CHECK ((ams_id >= 0 AND ams_id <= 7) OR ams_id = 255),
                        CONSTRAINT ck_spoolman_slot_tray_id_range CHECK (tray_id >= 0 AND tray_id <= 3)
                    )
                    """
                )
            )
        else:
            await conn.execute(
                text(
                    """
                    CREATE TABLE spoolman_slot_assignments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        ams_id INTEGER NOT NULL,
                        tray_id INTEGER NOT NULL,
                        spoolman_spool_id INTEGER NOT NULL,
                        assigned_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                        CONSTRAINT uq_spoolman_slot_assignment UNIQUE (printer_id, ams_id, tray_id),
                        CONSTRAINT ck_spoolman_slot_ams_id_range CHECK ((ams_id >= 0 AND ams_id <= 7) OR ams_id = 255),
                        CONSTRAINT ck_spoolman_slot_tray_id_range CHECK (tray_id >= 0 AND tray_id <= 3)
                    )
                    """
                )
            )

    # Table 2: spoolman_k_profile
    if not await table_exists(conn, "spoolman_k_profile"):
        if is_postgres():
            await conn.execute(
                text(
                    """
                    CREATE TABLE spoolman_k_profile (
                        id SERIAL PRIMARY KEY,
                        spoolman_spool_id INTEGER NOT NULL,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        extruder INTEGER NOT NULL DEFAULT 0,
                        nozzle_diameter VARCHAR(10) NOT NULL DEFAULT '0.4',
                        nozzle_type VARCHAR(50),
                        k_value DOUBLE PRECISION NOT NULL,
                        name VARCHAR(100),
                        cali_idx INTEGER,
                        setting_id VARCHAR(50),
                        created_at TIMESTAMP NOT NULL DEFAULT now(),
                        CONSTRAINT uq_spoolman_kp UNIQUE (spoolman_spool_id, printer_id, extruder, nozzle_diameter),
                        CONSTRAINT ck_spoolman_kp_extruder_range CHECK (extruder >= 0 AND extruder <= 1)
                    )
                    """
                )
            )
        else:
            await conn.execute(
                text(
                    """
                    CREATE TABLE spoolman_k_profile (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        spoolman_spool_id INTEGER NOT NULL,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        extruder INTEGER NOT NULL DEFAULT 0,
                        nozzle_diameter VARCHAR(10) NOT NULL DEFAULT '0.4',
                        nozzle_type VARCHAR(50),
                        k_value REAL NOT NULL,
                        name VARCHAR(100),
                        cali_idx INTEGER,
                        setting_id VARCHAR(50),
                        created_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                        CONSTRAINT uq_spoolman_kp UNIQUE (spoolman_spool_id, printer_id, extruder, nozzle_diameter),
                        CONSTRAINT ck_spoolman_kp_extruder_range CHECK (extruder >= 0 AND extruder <= 1)
                    )
                    """
                )
            )

    # Spools.storage_location (mirrors Spoolman's own location field)
    await add_column(conn, "spools", "storage_location VARCHAR(255)")

    # Widen spools.tag_uid to 32 chars (Postgres only — SQLite ignores VARCHAR length).
    # Idempotent: skips if the target type is already VARCHAR(32) or wider.
    if is_postgres():
        result = await conn.execute(
            text(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='spools' AND column_name='tag_uid'"
            )
        )
        current_len = result.scalar()
        if current_len is not None and current_len < 32:
            await conn.execute(text("ALTER TABLE spools ALTER COLUMN tag_uid TYPE VARCHAR(32)"))
