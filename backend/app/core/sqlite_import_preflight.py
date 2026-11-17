"""Validate the effective imported types; never coerce the backup's values."""

import json
import sqlite3
from contextlib import closing

from sqlalchemy import JSON, Column, String, UniqueConstraint

SOURCE_VALUES_MESSAGE = (
    "The backup contains text values incompatible with PostgreSQL. "
    "Correct a copy of the source and retry; no data has been replaced."
)
SOURCE_COLLISION_MESSAGE = (
    "The backup contains an unsupported constraint/index name collision. No data has been replaced."
)


class SqliteSourceValuesError(ValueError):
    code = "sqlite_source_text_incompatible"

    def __init__(self, violations):
        self.violations = violations
        super().__init__(SOURCE_VALUES_MESSAGE)


class SqliteSourceCollisionError(ValueError):
    code = "sqlite_source_index_collision"

    def __init__(self):
        super().__init__(SOURCE_COLLISION_MESSAGE)


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _json_nul(value):
    if isinstance(value, str):
        return "\0" in value
    if isinstance(value, dict):
        return any(_json_nul(k) or _json_nul(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_json_nul(item) for item in value)
    return False


def validate_text_values(path, metadata):
    """Use the SAME reflected PG types that create_all will emit.

    PostgreSQL tolerates overlength trailing spaces and json (not jsonb)
    tolerates escaped NUL. This boundary intentionally refuses both for
    lossless, cross-type portability. Values never enter diagnostics.
    """
    violations = []
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as source:
        for table in metadata.tables.values():
            columns = [c for c in table.c if isinstance(c.type, (String, JSON))]
            if not columns:
                continue
            counts = {}
            cursor = source.execute(f"SELECT {', '.join(_quote(c.name) for c in columns)} FROM {_quote(table.name)}")
            while rows := cursor.fetchmany(500):
                for row in rows:
                    for column, value in zip(columns, row, strict=True):
                        if value is None:
                            continue
                        reason = None
                        if isinstance(column.type, JSON):
                            if isinstance(value, (str, bytes)) and ("\0" if isinstance(value, str) else b"\0") in value:
                                reason = "nul"
                            elif isinstance(value, bytes):
                                reason = "invalid_json"
                            else:
                                try:
                                    decoded = json.loads(value) if isinstance(value, str) else value
                                    if _json_nul(decoded):
                                        reason = "nul"
                                except (ValueError, RecursionError):
                                    reason = "invalid_json"
                        elif not isinstance(value, str):
                            reason = "non_string"
                        elif "\0" in value:
                            reason = "nul"
                        elif column.type.length is not None and len(value) > column.type.length:
                            reason = "over_length"
                        if reason:
                            key = column.name, reason
                            counts[key] = counts.get(key, 0) + 1
            violations.extend(
                {"table": table.name, "column": column, "reason": reason, "count": count}
                for (column, reason), count in sorted(counts.items())
            )
    if violations:
        raise SqliteSourceValuesError(violations)


def omit_redundant_notification_index(table, source):
    """Only the exact m222 redundant UNIQUE. Unknown collisions fail closed."""
    name = "uq_user_notifications_dedupe_key"
    collisions = [i for i in table.indexes if i.name == name]
    constraints = [c for c in table.constraints if c.name == name]
    physical = [row for row in source.execute('PRAGMA index_list("user_notifications")') if row[1] == name]
    if not physical or not constraints:
        return
    # SQLAlchemy skips expression indexes during reflection. That is unknown
    # shape, not proof that the colliding physical index does not exist.
    if len(collisions) != 1 or len(constraints) != 1:
        raise SqliteSourceCollisionError()
    index, constraint = collisions[0], constraints[0]
    if (
        not isinstance(constraint, UniqueConstraint)
        or tuple(c.name for c in constraint.columns) != ("dedupe_key",)
        or not index.unique
        or len(index.expressions) != 1
        or not isinstance(index.expressions[0], Column)
        or index.expressions[0].name != "dedupe_key"
        or index.dialect_options["sqlite"].get("where") is not None
    ):
        raise SqliteSourceCollisionError()

    def key(index_name):
        return tuple((r[2], r[3], r[4]) for r in source.execute(f"PRAGMA index_xinfo({_quote(index_name)})") if r[5])

    expected = (("dedupe_key", 0, "BINARY"),)
    backing = [
        r[1]
        for r in source.execute('PRAGMA index_list("user_notifications")')
        if r[2] and r[3] == "u" and not r[4] and key(r[1]) == expected
    ]
    if not backing or key(name) != expected:
        raise SqliteSourceCollisionError()
    table.indexes.remove(index)
