"""The highest built-in spool number a database has ever handed out (C047).

SQLite without ``AUTOINCREMENT`` gives the next row ``max(id) + 1``, so deleting
the newest spool handed its number to the next one: a printed numeric label and
every row still naming the old number then pointed at a different spool. m261
makes ``spool`` an ``AUTOINCREMENT`` table, and its counter has to start above
every number the database still remembers — not only ``MAX(spool.id)``, which
forgets the deleted spool whose history, slot rows or label jobs outlived it.

The registry below is FROZEN at the schema C047 met. It is deliberately not
derived from the live models: the migration and the SQLite→PostgreSQL import
both read databases at OLDER levels, whose shape the models no longer describe.
A carrier added after C047 needs no entry — its numbers come from a counter that
already never goes back (``tests/unit/test_spool_id_bound.py`` says so).

Each carrier has a GUARANTEED level: the migration after which it must exist.
``None`` means no migration creates it — ``create_all`` did, at whatever version
the database first started — so its absence is never an error. A carrier that is
present and valid always counts, whatever the level says: ``create_all`` can bring
one before its migration (``active_print_sessions`` existed before m221).

The DDL type and the values are checked apart. A carrier of an unexpected type
stops the caller: a bound computed by skipping it would be a guess.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Carrier:
    table: str
    column: str
    kind: str  # "int" — the column holds spool numbers; "json" — {"<ams>-<tray>": <spool number>}
    guaranteed: int | None


CARRIERS: tuple[Carrier, ...] = (
    Carrier("spool", "id", "int", None),
    Carrier("spool_assignment", "spool_id", "int", None),
    Carrier("spool_k_profile", "spool_id", "int", None),
    Carrier("spool_usage_history", "spool_id", "int", None),
    Carrier("print_usage_events", "spool_id", "int", 153),
    Carrier("label_jobs", "spool_id", "int", 147),
    Carrier("active_print_sessions", "spool_assignments", "json", 221),
)


class SpoolIdBoundError(RuntimeError):
    """A carrier the bound must read is missing or has a shape this registry does not know."""


def json_spool_ids(raw) -> list[int]:
    """The spool numbers of one ``spool_assignments`` value; anything else is no number.

    Only positive integers count. A boolean is an ``int`` in Python and never a
    spool; strings, NULL, an empty object and a value that is not JSON are skipped.
    The keys are slot coordinates, never numbers.
    """
    if raw is None:
        return []
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, dict):
        return []
    return [v for v in raw.values() if isinstance(v, int) and not isinstance(v, bool) and v > 0]


def _missing(carrier: Carrier, applied: set[int], what: str) -> None:
    if carrier.guaranteed is not None and carrier.guaranteed in applied:
        raise SpoolIdBoundError(
            f"{carrier.table}.{carrier.column}: {what}, although migration {carrier.guaranteed} creates it"
        )


def _rows(conn, sql: str, params=()) -> list[tuple]:
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params)
        return list(cursor.fetchall())
    finally:
        cursor.close()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _sqlite_type_fits(kind: str, declared: str) -> bool:
    declared = (declared or "").upper()
    if kind == "int":
        return "INT" in declared  # SQLite's own INTEGER-affinity rule
    return declared == "JSON" or any(token in declared for token in ("TEXT", "CHAR", "CLOB"))


def sqlite_bound(conn, applied: set[int]) -> int | None:
    """The bound over a SQLite database, through any DB-API connection (sqlite3 or the aiosqlite adapter).

    Reads only. ``applied`` is the database's own ``_migrations`` versions.
    """
    best: int | None = None
    for carrier in CARRIERS:
        columns = {row[1]: row[2] for row in _rows(conn, f"PRAGMA table_xinfo({_quote(carrier.table)})")}
        if not columns:
            _missing(carrier, applied, "table is missing")
            continue
        if carrier.column not in columns:
            _missing(carrier, applied, "column is missing")
            continue
        if not _sqlite_type_fits(carrier.kind, columns[carrier.column]):
            raise SpoolIdBoundError(
                f"{carrier.table}.{carrier.column}: unexpected type {columns[carrier.column]!r} for a {carrier.kind} carrier"
            )
        table, column = _quote(carrier.table), _quote(carrier.column)
        if carrier.kind == "int":
            # SQLite keeps whatever was written: a text value in an INTEGER column
            # would sort above every number, so only real integers are compared.
            (value,) = _rows(
                conn,
                f"SELECT MAX({column}) FROM {table} WHERE typeof({column}) = 'integer' AND {column} > 0",  # noqa: S608
            )[0]
            values = [value] if value is not None else []
        else:
            values = [
                v
                for (raw,) in _rows(conn, f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL")  # noqa: S608
                for v in json_spool_ids(raw)
            ]
        if values:
            best = max(values) if best is None else max(best, *values)
    return best


_PG_INT_TYPES = {"smallint", "integer", "bigint"}
_PG_JSON_TYPES = {"json", "jsonb"}


async def postgres_bound(conn, applied: set[int]) -> int | None:
    """The bound over a PostgreSQL database, on an SQLAlchemy async connection. Reads only."""
    from sqlalchemy import text

    best: int | None = None
    for carrier in CARRIERS:
        result = await conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": carrier.table},
        )
        types = dict(result.tuples().all())
        if not types:
            _missing(carrier, applied, "table is missing")
            continue
        if carrier.column not in types:
            _missing(carrier, applied, "column is missing")
            continue
        wanted = _PG_INT_TYPES if carrier.kind == "int" else _PG_JSON_TYPES
        if types[carrier.column] not in wanted:
            raise SpoolIdBoundError(
                f"{carrier.table}.{carrier.column}: unexpected type {types[carrier.column]!r} for a {carrier.kind} carrier"
            )
        table, column = _quote(carrier.table), _quote(carrier.column)
        if carrier.kind == "int":
            value = (await conn.execute(text(f"SELECT MAX({column}) FROM {table} WHERE {column} > 0"))).scalar()  # noqa: S608
            values = [value] if value is not None else []
        else:
            rows = await conn.execute(text(f"SELECT {column}::text FROM {table} WHERE {column} IS NOT NULL"))  # noqa: S608
            values = [v for (raw,) in rows.all() for v in json_spool_ids(raw)]
        if values:
            best = max(values) if best is None else max(best, *values)
    return best


def sqlite_applied_versions(conn) -> set[int]:
    """The ``_migrations`` versions of a SQLite database; none when it has no such table."""
    if not _rows(conn, "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = '_migrations'"):
        return set()
    return {row[0] for row in _rows(conn, "SELECT version FROM _migrations")}


def sqlite_orphan_history(conn) -> int:
    """How many ``spool_usage_history`` rows name a spool that ``spool`` no longer holds. Reads only."""
    tables = {row[0] for row in _rows(conn, "SELECT name FROM sqlite_schema WHERE type = 'table'")}
    if not {"spool", "spool_usage_history"} <= tables:
        return 0
    return _rows(
        conn,
        "SELECT count(*) FROM spool_usage_history h WHERE h.spool_id IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM spool s WHERE s.id = h.spool_id)",
    )[0][0]
