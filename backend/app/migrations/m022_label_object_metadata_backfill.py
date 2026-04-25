"""Backfill ``gcode_label_objects`` + ``exclude_object`` into JSON metadata.

These two flags are extracted from ``Metadata/project_settings.config``
inside each 3MF (the slicer's print-profile JSON):

* ``gcode_label_objects`` — Orca writes it explicitly; Bambu Studio
  doesn't (it always emits ``label_object`` markers in gcode), so a
  missing field defaults to ``True`` to match Bambu Studio's behaviour.
* ``exclude_object`` — both slicers emit this. No fallback when missing
  (the design choice — uninterpretable values are simply skipped).

The fields land in:

* ``library_files.file_metadata`` (JSON column) — used to decide whether
  the skip-objects modal can ever offer per-object exclusion for a
  print started from this file.
* ``print_archives.extra_data`` (JSON column) — same, but for archives.

Forward path (new uploads + new prints): the parser changes in
``services/archive.py::ThreeMFParser._extract_print_settings`` push
both keys into ``self.metadata``, which is the same dict that flows
into ``file_metadata`` and ``extra_data`` everywhere downstream.

This migration is the **backfill** for installations that already have
library files + archive history. It opens every 3MF still on disk,
re-extracts the two fields, and merges them into the JSON column.
Files that have been deleted (history rows whose 3MF no longer exists)
are skipped silently — those rows just stay without the fields and the
skip-objects feature simply won't apply to them.

**Long-startup warning**: on installs with hundreds or thousands of
archived 3MFs this seed step opens each ZIP, reads one config file,
parses JSON, merges into the row. Expect 50-200 ms per file. CHANGELOG
entry mentions the one-time delay.
"""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

from sqlalchemy import text

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

version = 22
name = "label_object_metadata_backfill"


# Commit batch size — keeps the SQLite write-lock window short so a
# parallel reader (e.g. a startup ws_manager subscriber) doesn't time
# out. Tuned for "long but progressable" rather than "one big txn".
_BATCH_SIZE = 100


def _coerce_bool(value):
    """Same shape as services.archive._coerce_bool — copied here because
    migrations are frozen and shouldn't depend on importable code that
    can change after the migration is shipped."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
        return None
    if isinstance(value, list) and value:
        return _coerce_bool(value[0])
    return None


def _extract_label_object_fields(file_path: Path) -> dict | None:
    """Open *file_path* (3MF), read project_settings.config, return the
    two fields as a dict ready to merge.

    Returns None when the 3MF can't be opened or the config is
    corrupt — caller logs and moves on to the next row.
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/project_settings.config" not in zf.namelist():
                # No config inside — but we still emit the Bambu Studio
                # default for gcode_label_objects so the field at least
                # exists on the row (frontend gates can rely on it).
                return {"gcode_label_objects": True}
            content = zf.read("Metadata/project_settings.config").decode("utf-8", errors="replace")
            data = json.loads(content)
    except (zipfile.BadZipFile, OSError, json.JSONDecodeError, KeyError):
        return None
    except Exception as e:  # noqa: BLE001 — log + skip is the design
        logger.debug("m022: unexpected error reading %s: %s", file_path, e)
        return None

    out: dict = {}
    glo_raw = data.get("gcode_label_objects")
    glo = _coerce_bool(glo_raw)
    out["gcode_label_objects"] = True if glo is None else glo
    if "exclude_object" in data:
        eo = _coerce_bool(data["exclude_object"])
        if eo is not None:
            out["exclude_object"] = eo
    return out


async def upgrade(conn):
    """No DDL — both target columns already exist; we only fill JSON."""
    _ = conn  # noqa: ARG001


async def seed(session_factory):
    """Backfill JSON metadata in library_files + print_archives."""
    async with session_factory() as db:
        # ---------- library_files.file_metadata ----------
        result = await db.execute(
            text("SELECT id, file_path, file_metadata FROM library_files WHERE file_metadata IS NOT NULL")
        )
        rows = result.fetchall()
        lib_total = len(rows)
        lib_updated = 0
        lib_already = 0
        lib_missing_file = 0
        lib_unreadable = 0
        pending = 0

        for row_id, file_path, file_metadata in rows:
            try:
                meta = json.loads(file_metadata) if isinstance(file_metadata, str) else (file_metadata or {})
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(meta, dict):
                continue
            # Skip rows that already have both fields — idempotent on re-runs
            # (which the _migrations table prevents anyway, but cheap to be safe).
            if "gcode_label_objects" in meta and "exclude_object" in meta:
                lib_already += 1
                continue
            if not file_path:
                lib_missing_file += 1
                continue
            disk_path = Path(file_path) if Path(file_path).is_absolute() else settings.base_dir / file_path
            if not disk_path.is_file():
                lib_missing_file += 1
                continue
            extracted = _extract_label_object_fields(disk_path)
            if extracted is None:
                lib_unreadable += 1
                continue
            new_meta = {**meta, **extracted}
            await db.execute(
                text("UPDATE library_files SET file_metadata = :m WHERE id = :id"),
                {"m": json.dumps(new_meta), "id": row_id},
            )
            lib_updated += 1
            pending += 1
            if pending >= _BATCH_SIZE:
                await db.commit()
                pending = 0
                logger.info(
                    "m022 library_files: progress %d/%d (updated=%d, missing=%d, unreadable=%d)",
                    lib_already + lib_updated + lib_missing_file + lib_unreadable,
                    lib_total,
                    lib_updated,
                    lib_missing_file,
                    lib_unreadable,
                )
        if pending:
            await db.commit()
            pending = 0
        logger.info(
            "m022 library_files done: total=%d updated=%d already=%d missing=%d unreadable=%d",
            lib_total,
            lib_updated,
            lib_already,
            lib_missing_file,
            lib_unreadable,
        )

        # ---------- print_archives.extra_data ----------
        result = await db.execute(
            text(
                "SELECT id, file_path, extra_data FROM print_archives "
                "WHERE file_path IS NOT NULL AND file_path != '' "
                "AND extra_data IS NOT NULL"
            )
        )
        rows = result.fetchall()
        arch_total = len(rows)
        arch_updated = 0
        arch_already = 0
        arch_missing_file = 0
        arch_unreadable = 0

        for row_id, file_path, extra_data in rows:
            try:
                meta = json.loads(extra_data) if isinstance(extra_data, str) else (extra_data or {})
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(meta, dict):
                continue
            if "gcode_label_objects" in meta and "exclude_object" in meta:
                arch_already += 1
                continue
            disk_path = settings.base_dir / file_path
            if not disk_path.is_file():
                arch_missing_file += 1
                continue
            extracted = _extract_label_object_fields(disk_path)
            if extracted is None:
                arch_unreadable += 1
                continue
            new_meta = {**meta, **extracted}
            await db.execute(
                text("UPDATE print_archives SET extra_data = :m WHERE id = :id"),
                {"m": json.dumps(new_meta), "id": row_id},
            )
            arch_updated += 1
            pending += 1
            if pending >= _BATCH_SIZE:
                await db.commit()
                pending = 0
                logger.info(
                    "m022 print_archives: progress %d/%d (updated=%d, missing=%d, unreadable=%d)",
                    arch_already + arch_updated + arch_missing_file + arch_unreadable,
                    arch_total,
                    arch_updated,
                    arch_missing_file,
                    arch_unreadable,
                )
        if pending:
            await db.commit()
        logger.info(
            "m022 print_archives done: total=%d updated=%d already=%d missing=%d unreadable=%d",
            arch_total,
            arch_updated,
            arch_already,
            arch_missing_file,
            arch_unreadable,
        )
