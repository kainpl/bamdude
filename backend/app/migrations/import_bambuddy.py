"""Import data from a legacy Bambuddy/BambuTrack database into a fresh BamDude database.

Opens the old DB read-only via aiosqlite, copies rows through a generic
_import_table() helper, and applies per-table transforms where the schema has
changed.  The old database is NEVER modified.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiosqlite
from sqlalchemy import text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Table lists
# ---------------------------------------------------------------------------

_DIRECT_COPY = [
    "settings",
    "filaments",
    "spool_catalog",
    "color_catalog",
    "notification_templates",
    "spool_assignment",
    "spool_k_profile",
    "kprofile_notes",
    "ams_sensor_history",
    "external_links",
    "orca_base_profiles",
    "api_keys",
    "git_backup_config",
    "git_backup_logs",
    "local_presets",
    "notification_logs",
    "slot_preset_mappings",
]

_COPY_WITH_DEFAULTS: dict[str, dict[str, Any]] = {
    "printers": {
        "stagger_interval_minutes": 0,
        "swap_mode_enabled": 0,
        "auto_light_off": 0,
        "plate_detection_enabled": 0,
        "cleanup_after_print": 1,
        "mqtt_connection_timeout": 300,
    },
    "print_archives": {
        "swap_compatible": 0,
        "created_by_id": None,
        "quantity": 1,
    },
    "smart_plugs": {
        "plug_type": "tasmota",
        "auto_off_persistent": 0,
        "show_in_switchbar": 0,
    },
    "notification_providers": {
        "on_print_stopped": 1,
        "on_maintenance_due": 0,
        "on_print_missing_spool_assignment": 0,
        "on_ams_humidity_high": 0,
        "on_ams_temperature_high": 0,
        "on_plate_not_empty": 1,
        "on_bed_cooled": 0,
        "on_first_layer_complete": 0,
        "on_queue_job_added": 0,
        "on_queue_job_started": 0,
        "on_queue_job_waiting": 1,
        "on_queue_job_skipped": 1,
        "on_queue_job_failed": 1,
        "on_queue_completed": 0,
    },
    "library_folders": {
        "is_external": 0,
        "external_readonly": 0,
    },
    "library_files": {
        "is_external": 0,
        "swap_compatible": 0,
    },
    "maintenance_types": {
        "printer_models": '["*"]',
        "is_deleted": 0,
    },
    "printer_maintenance": {
        "custom_interval_type": None,
    },
    "maintenance_history": {
        "performed_by_user_id": None,
        "performed_by_chat_id": None,
    },
    "projects": {
        "priority": "normal",
        "is_template": 0,
    },
    "spool": {
        "data_origin": None,
        "cost_per_kg": None,
        "weight_locked": 0,
    },
    "spool_usage_history": {
        "cost": None,
        "archive_id": None,
    },
    "telegram_chats": {
        "daily_digest": 0,
        "quiet_hours_enabled": 0,
    },
}

_SKIP_TABLES = {
    "notification_templates",
    "spool_catalog",
    "color_catalog",
    "pending_uploads",
    "notification_digest_queue",
    "bug_reports",
}

_CONDITIONAL_TABLES = [
    "users",
    "groups",
    "user_groups",
    "user_email_preferences",
    "active_print_spoolman",
]

# ---------------------------------------------------------------------------
# Generic helper
# ---------------------------------------------------------------------------


async def _import_table(
    new_conn,
    old_db: aiosqlite.Connection,
    table: str,
    columns: list[str] | None = None,
    defaults: dict[str, Any] | None = None,
    transform: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    rename: dict[str, str] | None = None,
    source_table: str | None = None,
) -> int:
    """Copy rows from *old_db* ``source_table`` (or ``table``) into *new_conn* ``table``.

    Returns the number of rows inserted.
    """
    src = source_table or table
    # 1. Check source table exists
    async with old_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (src,),
    ) as cur:
        if not await cur.fetchone():
            return 0

    # 2. Source columns
    async with old_db.execute(f"PRAGMA table_info({src})") as cur:  # noqa: S608
        src_cols = [row[1] for row in await cur.fetchall()]
    if not src_cols:
        return 0

    # 3. Destination columns
    result = await new_conn.execute(text(f"PRAGMA table_info({table})"))
    dst_cols = [row[1] for row in result.fetchall()]
    if not dst_cols:
        return 0

    # 4. Build effective rename map
    renames = rename or {}

    # 5. Determine which source columns to select
    if columns is not None:
        # Explicit column list — use only those present in source
        select_cols = [c for c in columns if c in src_cols]
    else:
        # Auto-detect: source columns whose (possibly renamed) name exists in dest
        select_cols = [c for c in src_cols if renames.get(c, c) in dst_cols]

    if not select_cols:
        return 0

    # 6. Read all source rows
    col_list = ", ".join(select_cols)
    async with old_db.execute(f"SELECT {col_list} FROM {src}") as cur:  # noqa: S608
        rows = await cur.fetchall()
    if not rows:
        return 0

    # 7. Build rows as dicts, apply rename + defaults + transform
    count = 0
    for row in rows:
        record: dict[str, Any] = {}
        for idx, col in enumerate(select_cols):
            dest_col = renames.get(col, col)
            record[dest_col] = row[idx]

        # Apply defaults for columns present in destination but missing from record
        if defaults:
            for col, val in defaults.items():
                if col not in record and col in dst_cols:
                    record[col] = val

        # Apply per-row transform
        if transform:
            record = transform(record)
            if record is None:
                continue

        # Filter to only destination columns
        record = {k: v for k, v in record.items() if k in dst_cols}

        if not record:
            continue

        # 8. INSERT
        ins_cols = list(record.keys())
        placeholders = ", ".join(f":{c}" for c in ins_cols)
        col_names = ", ".join(ins_cols)
        await new_conn.execute(
            text(f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"),
            record,
        )
        count += 1

    return count


# ---------------------------------------------------------------------------
# Per-table transform functions
# ---------------------------------------------------------------------------


def _transform_ams_labels(row: dict[str, Any]) -> dict[str, Any]:
    """Old schema: (printer_id, ams_id).  New: ams_serial_number UNIQUE."""
    printer_id = row.pop("printer_id", 0)
    ams_id = row.pop("ams_id", row.get("ams_id", 0))
    # Keep ams_id as a hint column if present in dest
    row["ams_id"] = ams_id
    row["ams_serial_number"] = f"p{printer_id}a{ams_id}"
    return row


def _transform_virtual_printer(row: dict[str, Any]) -> dict[str, Any]:
    """Fix SSDP model codes and add auto_dispatch default."""
    row.setdefault("auto_dispatch", 1)
    return row


def _transform_project_bom(row: dict[str, Any]) -> dict[str, Any]:
    """Rename columns: quantity_printed -> quantity_acquired, notes -> remarks."""
    # Renames are handled by the rename parameter, so this is a no-op safety net.
    return row


def _make_queue_transform(printer_queue_map: dict[int, int]) -> Callable:
    """Return a transform that sets queue_id from the printer_id."""

    def _transform(row: dict[str, Any]) -> dict[str, Any] | None:
        printer_id = row.pop("printer_id", None)
        if printer_id is None:
            return None
        queue_id = printer_queue_map.get(printer_id)
        if queue_id is None:
            return None
        row["queue_id"] = queue_id
        row.setdefault("require_previous_success", 0)
        return row

    return _transform


def _transform_macro(row: dict[str, Any]) -> dict[str, Any]:
    """Convert old printer_model (single string) to printer_models (JSON array)."""
    if "printer_model" in row:
        model = row.pop("printer_model")
        if model:
            row["printer_models"] = json.dumps([model])
        else:
            row["printer_models"] = '["*"]'
    return row


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def import_bambuddy_data(engine, legacy_db_path: Path) -> None:
    """Import data from a legacy Bambuddy / BambuTrack database.

    The new database must already have all tables created via ``create_all()``.
    The old database is opened read-only and is never modified.
    """
    summary: dict[str, int] = {}

    old_db = await aiosqlite.connect(f"file:{legacy_db_path}?mode=ro", uri=True)
    try:
        async with engine.begin() as conn:
            # ---------------------------------------------------------------
            # Phase 1: Printers (other tables reference them)
            # ---------------------------------------------------------------
            n = await _import_table(
                conn,
                old_db,
                "printers",
                defaults=_COPY_WITH_DEFAULTS["printers"],
            )
            summary["printers"] = n
            logger.info("Imported %d printers", n)

            # ---------------------------------------------------------------
            # Phase 2: Create printer_queues (one per printer)
            # ---------------------------------------------------------------
            await conn.execute(
                text(
                    "INSERT INTO printer_queues "
                    "(id, printer_id, status, pending_count, completed_count, "
                    "failed_count, cancelled_count, skipped_count, total_count) "
                    "SELECT id, id, 'idle', 0, 0, 0, 0, 0, 0 FROM printers"
                )
            )
            # Build printer_id -> queue_id mapping (queue_id == printer_id here)
            result = await conn.execute(text("SELECT id, printer_id FROM printer_queues"))
            printer_queue_map = {row[1]: row[0] for row in result.fetchall()}
            logger.info("Created %d printer queues", len(printer_queue_map))

            # ---------------------------------------------------------------
            # Phase 3: Independent tables — direct copy
            # ---------------------------------------------------------------
            # Tables renamed since Bambuddy — map new name -> old source name
            _table_renames = {
                "git_backup_config": "github_backup_config",
                "git_backup_logs": "github_backup_logs",
            }
            # Defaults for columns that exist in new tables but not in old
            _import_defaults = {
                "git_backup_config": {"provider": "github", "backup_spools": 0, "backup_archives": 0},
            }
            for tbl in _DIRECT_COPY:
                src = _table_renames.get(tbl)
                defs = _import_defaults.get(tbl)
                n = await _import_table(conn, old_db, tbl, source_table=src, defaults=defs)
                if n:
                    summary[tbl] = n
                    logger.info("Imported %d rows into %s", n, tbl)

            # ---------------------------------------------------------------
            # Phase 4: Tables with defaults for new columns
            # ---------------------------------------------------------------
            for tbl, defs in _COPY_WITH_DEFAULTS.items():
                if tbl == "printers":
                    continue  # already imported
                n = await _import_table(conn, old_db, tbl, defaults=defs)
                if n:
                    summary[tbl] = n
                    logger.info("Imported %d rows into %s", n, tbl)

            # ---------------------------------------------------------------
            # Phase 5: Tables with transforms
            # ---------------------------------------------------------------

            # ams_labels — schema change (printer_id, ams_id) -> ams_serial_number
            n = await _import_table(
                conn,
                old_db,
                "ams_labels",
                transform=_transform_ams_labels,
            )
            if n:
                summary["ams_labels"] = n
                logger.info("Imported %d rows into ams_labels", n)

            # project_bom_items — column renames
            n = await _import_table(
                conn,
                old_db,
                "project_bom_items",
                rename={"quantity_printed": "quantity_acquired", "notes": "remarks"},
            )
            if n:
                summary["project_bom_items"] = n
                logger.info("Imported %d rows into project_bom_items", n)

            # virtual_printers — model code fix + auto_dispatch default
            n = await _import_table(
                conn,
                old_db,
                "virtual_printers",
                defaults={"auto_dispatch": 1},
                transform=_transform_virtual_printer,
            )
            if n:
                summary["virtual_printers"] = n
                logger.info("Imported %d rows into virtual_printers", n)

            # print_queue — needs queue_id from printer_queue_map
            n = await _import_table(
                conn,
                old_db,
                "print_queue",
                transform=_make_queue_transform(printer_queue_map),
            )
            if n:
                summary["print_queue"] = n
                logger.info("Imported %d rows into print_queue", n)

            # macros — conditional, may not exist; printer_model -> printer_models
            n = await _import_table(
                conn,
                old_db,
                "macros",
                transform=_transform_macro,
            )
            if n:
                summary["macros"] = n
                logger.info("Imported %d rows into macros", n)

            # ---------------------------------------------------------------
            # Phase 6: Conditional tables (may not exist in old DB)
            # ---------------------------------------------------------------
            _conditional_defaults = {
                "users": {"auth_source": "local"},
            }
            for tbl in _CONDITIONAL_TABLES:
                n = await _import_table(conn, old_db, tbl, defaults=_conditional_defaults.get(tbl))
                if n:
                    summary[tbl] = n
                    logger.info("Imported %d rows into %s", n, tbl)

            # FTS index will be populated by m001 upgrade (archive_fts created there)

            # ---------------------------------------------------------------
            # Phase 8: Recount queue counters
            # ---------------------------------------------------------------
            if summary.get("print_queue", 0) > 0:
                await conn.execute(
                    text(
                        "UPDATE printer_queues SET "
                        "pending_count = (SELECT COUNT(*) FROM print_queue "
                        "  WHERE queue_id = printer_queues.id AND status = 'pending'), "
                        "completed_count = (SELECT COUNT(*) FROM print_queue "
                        "  WHERE queue_id = printer_queues.id AND status = 'completed'), "
                        "failed_count = (SELECT COUNT(*) FROM print_queue "
                        "  WHERE queue_id = printer_queues.id AND status = 'failed'), "
                        "cancelled_count = (SELECT COUNT(*) FROM print_queue "
                        "  WHERE queue_id = printer_queues.id AND status = 'cancelled'), "
                        "skipped_count = (SELECT COUNT(*) FROM print_queue "
                        "  WHERE queue_id = printer_queues.id AND status = 'skipped'), "
                        "total_count = (SELECT COUNT(*) FROM print_queue "
                        "  WHERE queue_id = printer_queues.id)"
                    )
                )
                logger.info("Recounted printer queue counters")

            # ---------------------------------------------------------------
            # Phase 9: Backfill maintenance history performer
            # ---------------------------------------------------------------
            if summary.get("maintenance_history", 0) > 0:
                await conn.execute(
                    text(
                        "UPDATE maintenance_history SET performed_by_user_id = ("
                        "  SELECT id FROM users ORDER BY id LIMIT 1"
                        ") WHERE performed_by_user_id IS NULL AND performed_by_chat_id IS NULL"
                    )
                )
                logger.info("Backfilled maintenance history performer")

    finally:
        await old_db.close()

    # ---------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------
    total_rows = sum(summary.values())
    logger.info(
        "Legacy import complete: %d tables, %d total rows",
        len(summary),
        total_rows,
    )
    for tbl, cnt in sorted(summary.items()):
        logger.info("  %-30s %d rows", tbl, cnt)
