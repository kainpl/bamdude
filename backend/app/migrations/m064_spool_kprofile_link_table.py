"""Convert ``spool_k_profile`` + ``spoolman_k_profile`` into thin link tables.

Background: K-profile data (``k_value``, ``name``, ``cali_idx``, ``setting_id``,
``nozzle_type``) was previously duplicated on every spool that referenced the
same printer-side calibration. 100 generic-PETG spools all carried the same
0.025 K row. After m062/m063 ``filament_calibration`` exists as the per-printer
cache; spool→K becomes a single FK.

What this migration does (per table):
  1. Add ``filament_calibration_id`` column (nullable initially).
  2. For each existing row, derive ``filament_id`` from ``setting_id`` via
     :func:`setting_id_to_filament_id`. Compute ``nozzle_volume_type`` from the
     2-char prefix of ``nozzle_type`` (``HS``→standard, ``HH``→high_flow, …).
  3. Find-or-create a ``filament_calibration`` row keyed by
     ``(printer_id, filament_id, nozzle_diameter, nozzle_volume_type,
     extruder_id, pa_k_value)``. Exact ``k_value`` match avoids violating the
     partial unique on ``is_active=True``: new rows ship as ``is_active=False``
     so user-managed activation stays explicit.
  4. Set ``filament_calibration_id`` on the link row.
  5. Drop rows where derivation failed (``setting_id`` NULL, etc.) with a log
     warning — those were already orphaned in practice.
  6. Drop OLD K-data columns: ``k_value``, ``name``, ``cali_idx``,
     ``setting_id``, ``nozzle_type``, ``nozzle_diameter``.

Idempotent: guarded by ``column_exists``.
"""

import logging

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import column_exists, table_exists
from backend.app.utils.filament_ids import setting_id_to_filament_id

logger = logging.getLogger(__name__)

version = 64
name = "spool_kprofile_link_table"


_NOZZLE_PREFIX_TO_VOL_TYPE = {
    "HS": "standard",
    "HH": "high_flow",
    "HU": "tpu_high_flow",
    "HY": "hybrid",
}


def _parse_nozzle_vol_type(nozzle_type: str | None) -> str:
    if not nozzle_type:
        return "standard"
    prefix = nozzle_type[:2] if len(nozzle_type) >= 2 else ""
    return _NOZZLE_PREFIX_TO_VOL_TYPE.get(prefix, "standard")


def _derive_filament_id(setting_id: str | None) -> str | None:
    if not setting_id:
        return None
    base = setting_id.split("_")[0] if "_" in setting_id else setting_id
    fid = setting_id_to_filament_id(base)
    return fid or None


async def _convert_table(conn, table: str) -> None:
    if not await table_exists(conn, table):
        return
    # Already migrated?
    if await column_exists(conn, table, "filament_calibration_id") and not await column_exists(conn, table, "k_value"):
        return

    # 1. Add the new column (nullable initially so backfill can proceed)
    if not await column_exists(conn, table, "filament_calibration_id"):
        if is_postgres():
            await conn.execute(
                text(
                    f"ALTER TABLE {table} ADD COLUMN filament_calibration_id INTEGER "
                    "REFERENCES filament_calibration(id) ON DELETE CASCADE"
                )
            )
        else:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN filament_calibration_id INTEGER"))

    # 2. Backfill: read all rows, find-or-create FC, set FK.
    rows = (
        (
            await conn.execute(
                text(
                    f"SELECT id, printer_id, extruder, nozzle_diameter, nozzle_type, "
                    f"k_value, name, cali_idx, setting_id FROM {table}"
                )
            )
        )
        .mappings()
        .all()
    )

    dropped = 0
    for r in rows:
        setting_id = r["setting_id"]
        filament_id = _derive_filament_id(setting_id)
        if not filament_id:
            dropped += 1
            continue

        try:
            nozzle_dia = float(r["nozzle_diameter"] or "0.4")
        except (TypeError, ValueError):
            nozzle_dia = 0.4
        vol_type = _parse_nozzle_vol_type(r["nozzle_type"])
        extruder_id = int(r["extruder"] or 0)
        k_value = r["k_value"]
        if k_value is None:
            dropped += 1
            continue

        # 3. Find existing fc row with EXACT K match (so many spools sharing the
        # same K=0.025 collapse to one shared fc row).
        existing = (
            await conn.execute(
                text(
                    "SELECT id FROM filament_calibration "
                    "WHERE printer_id = :pid AND filament_id = :fid "
                    "AND nozzle_diameter = :nd AND nozzle_volume_type = :vt "
                    "AND extruder_id = :ext AND pa_k_value = :kv"
                ),
                {
                    "pid": r["printer_id"],
                    "fid": filament_id,
                    "nd": nozzle_dia,
                    "vt": vol_type,
                    "ext": extruder_id,
                    "kv": float(k_value),
                },
            )
        ).scalar()

        if existing is None:
            # 4. Create new fc row as inactive — user can promote later via UI.
            display_name = r["name"] or f"{filament_id} K={float(k_value):.4f}"
            result = await conn.execute(
                text(
                    "INSERT INTO filament_calibration "
                    "(printer_id, filament_id, filament_setting_id, nozzle_diameter, "
                    "nozzle_volume_type, extruder_id, pa_k_value, cali_mode, source, "
                    "is_active, cali_idx, name, created_at) "
                    "VALUES (:pid, :fid, :sid, :nd, :vt, :ext, :kv, 'pa_line', "
                    f"'m064_backfill', {('FALSE' if is_postgres() else '0')}, :ci, :nm, "
                    f"{('NOW()' if is_postgres() else 'CURRENT_TIMESTAMP')}) "
                    + ("RETURNING id" if is_postgres() else "")
                ),
                {
                    "pid": r["printer_id"],
                    "fid": filament_id,
                    "sid": setting_id,
                    "nd": nozzle_dia,
                    "vt": vol_type,
                    "ext": extruder_id,
                    "kv": float(k_value),
                    "ci": r["cali_idx"],
                    "nm": display_name,
                },
            )
            if is_postgres():
                fc_id = result.scalar()
            else:
                fc_id = (await conn.execute(text("SELECT last_insert_rowid()"))).scalar()
        else:
            fc_id = existing

        await conn.execute(
            text(f"UPDATE {table} SET filament_calibration_id = :fcid WHERE id = :rid"),
            {"fcid": fc_id, "rid": r["id"]},
        )

    if dropped:
        logger.warning(
            "m064: dropping %d %s rows with un-derivable filament_id (setting_id NULL/blank).",
            dropped,
            table,
        )

    # 5. Delete rows where backfill failed.
    await conn.execute(text(f"DELETE FROM {table} WHERE filament_calibration_id IS NULL"))

    # 6. Drop OLD columns. Both Postgres + SQLite 3.35+ support DROP COLUMN.
    for col in ("k_value", "name", "cali_idx", "setting_id", "nozzle_type", "nozzle_diameter"):
        if await column_exists(conn, table, col):
            await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {col}"))


async def upgrade(conn) -> None:
    await _convert_table(conn, "spool_k_profile")
    await _convert_table(conn, "spoolman_k_profile")
