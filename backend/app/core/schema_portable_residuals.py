"""Omit only proven unenforceable guards on a PRIVATE portable metadata copy."""

import json
import sqlite3
from contextlib import closing

from sqlalchemy import CheckConstraint, UniqueConstraint

from backend.app.core.schema_conform_repair import data_residuals
from backend.app.core.schema_repair_state import SchemaRepairPending


async def adapt_portable(conn, portable, models):
    # Called only INSIDE the same REPEATABLE READ transaction that reads rows.
    # Export is not consent to restore; its report grants no target authority.
    reports = await data_residuals(conn, models, accepted_calibration=True)
    for report in reports:
        name, kind, target = report["object"].split(".", 2)
        table = portable.tables[name]
        if kind == "column" and target.endswith(".nullable"):
            table.c[target.removesuffix(".nullable")].nullable = True
        elif kind == "indexes":
            matches = [i for i in table.indexes if i.name == target]
            if len(matches) != 1:
                raise SchemaRepairPending("ambiguous_export_index")
            table.indexes.remove(matches[0])
        elif kind == "check":
            matches = [c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == target]
            if len(matches) != 1:
                raise SchemaRepairPending("ambiguous_export_check")
            table.constraints.remove(matches[0])
        elif kind in ("unique", "foreign_keys") and target.startswith("columns:"):
            columns = target.removeprefix("columns:").split(",")
            matches = [
                c
                for c in table.constraints
                if [col.name for col in c.columns] == columns
                and (isinstance(c, UniqueConstraint) if kind == "unique" else c in table.foreign_key_constraints)
            ]
            if len(matches) != 1:
                raise SchemaRepairPending("ambiguous_export_constraint")
            table.constraints.remove(matches[0])
        else:
            raise SchemaRepairPending("unknown_export_residual")
    return reports


def snapshot_warning_count(path):
    """A diagnostic only; no restore decision ever trusts this summary."""
    from backend.app.core.schema_repair_state import PORTABLE

    if not path.exists():
        return 0
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as source:
            rows = source.execute('SELECT value FROM settings WHERE "key"=?', (PORTABLE,)).fetchall()
        if len(rows) != 1:
            return 0
        value = json.loads(rows[0][0])
        return len(value["entries"]) if isinstance(value, dict) and isinstance(value.get("entries"), list) else 0
    except (sqlite3.Error, ValueError, TypeError):
        # Generic file/manifest tests and legacy snapshots have no such table.
        # This field is informational and does not substitute preflight proof.
        return 0


def zip_warning_count(path):
    import zipfile

    with zipfile.ZipFile(path) as archive:
        value = json.loads(archive.read("backup-manifest.json"))
    count = value.get("schema_residual_objects", 0)
    return count if type(count) is int and count >= 0 else 0
