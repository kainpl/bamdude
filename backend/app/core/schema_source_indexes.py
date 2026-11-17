"""Reject unknown current-shape index collisions on the owned source copy."""

import sqlite3
from contextlib import closing

from backend.app.core.schema_index_repair import CALIBRATION, manifest, quote, sqlite_index_shape

SOURCE_INDEX_MESSAGE = "Backup contains an unrecognized application index definition. No data was replaced."


class SourceIndexShapeError(ValueError):
    code = "sqlite_source_index_incompatible"
    message = SOURCE_INDEX_MESSAGE

    def __init__(self):
        super().__init__(self.message)


def validate_source_indexes(path):
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        for row in manifest():
            actual = source.execute(
                "SELECT type,tbl_name,sql FROM sqlite_schema WHERE name=?", (row["name"],)
            ).fetchone()
            if actual is None:
                continue
            if actual["type"] != "index" or actual["tbl_name"] != row["table"]:
                raise SourceIndexShapeError()
            columns = {r["name"] for r in source.execute(f"PRAGMA table_info({quote(row['table'])})")}
            needed = set(row["columns"])
            if row["name"] == "ix_print_archives_effective_hash":
                needed.add("content_hash")
            indexes = source.execute(f"PRAGMA index_list({quote(row['table'])})").fetchall()
            flags = next((r for r in indexes if r["name"] == row["name"]), None)
            keys = source.execute(f"PRAGMA index_xinfo({quote(row['name'])})").fetchall()
            if not needed <= columns:
                # Only the released pre-m063 calibration key is a known old
                # same-name shape, and that converter must still be pending.
                has_history = source.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='_migrations'"
                ).fetchone()
                level = source.execute("SELECT max(version) FROM _migrations").fetchone()[0] if has_history else 0
                if level is None:
                    level = 0
                if row["name"] == CALIBRATION and type(level) is int and level < 63:
                    historical = {**row, "columns": [c for c in row["columns"] if c != "printer_id"]}
                    if sqlite_index_shape(historical, actual, flags, keys) == "correct":
                        continue
                raise SourceIndexShapeError()
            if sqlite_index_shape(row, actual, flags, keys) == "unknown":
                raise SourceIndexShapeError()
