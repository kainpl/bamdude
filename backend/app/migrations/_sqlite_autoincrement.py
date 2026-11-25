"""Give a SQLite table ``AUTOINCREMENT`` in place, owning the whole transaction (C047, spec §A).

SQLite cannot add ``AUTOINCREMENT`` to an existing table: the table is rebuilt
under its own name. The runner hands this helper ONE pooled connection with no
transaction open on it and no ``PRAGMA foreign_keys`` toggle of its own
(``migrations/__init__.py``, ``sqlite_owns_transaction``) — the pragma does not
apply inside a transaction, so only the owner of the transaction can set and
restore it. Everything below runs on that DB-API connection, synchronously:
``sqlite3`` directly, or SQLAlchemy's aiosqlite adapter inside ``run_sync``.

Steps (spec §A): foreign keys off outside the transaction; ``BEGIN IMMEDIATE``;
every schema read and check under it, before the first change; copy into a
staging table rebuilt from the original text; ``DROP`` → ``RENAME`` with
``legacy_alter_table`` (views and other tables' triggers name the table and
must not be rewritten while it is momentarily missing); the table's own
indexes and triggers recreated from their saved text; the counter written; the
foreign-key violations compared with the snapshot; ``COMMIT``. Then the pragma
is restored and read back. A failure before ``COMMIT`` rolls everything back
and leaves no staging table; a failure after it leaves an ``AUTOINCREMENT``
table whose next run only raises the counter.

Only two DDL shapes are accepted, the two BamDude ever wrote: ``create_all``'s
``id INTEGER NOT NULL,`` with a table-level ``PRIMARY KEY (id)``, and an inline
``id INTEGER [NOT NULL] PRIMARY KEY``. Anything else stops before any change:
a rebuild from a shape we have not proven would be a guess about the user's data.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


class SelfManagedFailure(RuntimeError):
    """The rebuild did not complete. ``connection_safe`` is False when the rollback or the
    restore of the connection's pragmas could not be proven — the runner then discards the
    connection instead of returning it to the pool."""

    def __init__(self, message: str, *, connection_safe: bool):
        super().__init__(message)
        self.connection_safe = connection_safe


class UnsupportedShape(RuntimeError):
    """The table's stored shape is not one this helper rebuilds; nothing was changed."""


@dataclass(frozen=True)
class Outcome:
    rebuilt: bool
    sequence: int | None
    connection_safe: bool


Fault = Callable[[str], None]

_AUTOINCREMENT = re.compile(r"\bAUTOINCREMENT\b", re.IGNORECASE)
_TABLE_PK = re.compile(r",\s*PRIMARY\s+KEY\s*\(\s*\"?id\"?\s*\)", re.IGNORECASE)
_CONSTRAINT_PK = re.compile(r"\bCONSTRAINT\b[^,]*\bPRIMARY\s+KEY\b", re.IGNORECASE)


def _rows(conn, sql: str, params=()) -> list[tuple]:
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params)
        return list(cursor.fetchall())
    finally:
        cursor.close()


def _run(conn, sql: str, params=()) -> None:
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params)
    finally:
        cursor.close()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _in_transaction(conn) -> bool:
    flag = getattr(conn, "in_transaction", None)
    if isinstance(flag, bool):
        return flag
    return bool(conn.driver_connection.in_transaction)  # SQLAlchemy's aiosqlite adapter


def _xinfo(conn, table: str) -> list[tuple]:
    return _rows(conn, f"PRAGMA table_xinfo({_quote(table)})")


def _split_ddl(sql: str, table: str) -> tuple[str, str]:
    """``CREATE TABLE <name> (`` and the rest, or UnsupportedShape."""
    head = re.match(r"\s*CREATE\s+TABLE\s+(\"?)" + re.escape(table) + r"\1\s*\(", sql, re.IGNORECASE)
    if head is None:
        raise UnsupportedShape(f"{table}: unexpected CREATE TABLE header")
    return sql[: head.end()], sql[head.end() :]


def _id_column(body: str, table: str) -> re.Match:
    """The ``id`` column definition: first in the list, as BamDude always wrote it."""
    match = re.match(r"\s*(\"?id\"?)\s+INTEGER\b(?P<rest>[^,()]*)(?=,|\)\s*$)", body, re.IGNORECASE)
    if match is None:
        raise UnsupportedShape(f"{table}: id is not the first column, INTEGER, in a known form")
    return match


def _shape(sql: str, table: str) -> tuple[str, bool]:
    """``("table_pk" | "inline", already_autoincrement)`` for an accepted DDL, else UnsupportedShape."""
    _head, body = _split_ddl(sql, table)
    column = _id_column(body, table)
    rest = " ".join(column.group("rest").upper().split())
    if _CONSTRAINT_PK.search(body):
        raise UnsupportedShape(f"{table}: a named PRIMARY KEY constraint")
    if re.search(r"\bWITHOUT\s+ROWID\b", body, re.IGNORECASE):
        raise UnsupportedShape(f"{table}: a WITHOUT ROWID table")
    table_pk = len(_TABLE_PK.findall(body))
    autoinc = len(_AUTOINCREMENT.findall(sql))
    if rest in ("NOT NULL", "") and table_pk == 1 and autoinc == 0:
        return "table_pk", False
    if rest in ("PRIMARY KEY", "NOT NULL PRIMARY KEY") and table_pk == 0 and autoinc == 0:
        return "inline", False
    if rest in ("PRIMARY KEY AUTOINCREMENT", "NOT NULL PRIMARY KEY AUTOINCREMENT") and table_pk == 0 and autoinc == 1:
        return "inline", True
    raise UnsupportedShape(f"{table}: unsupported id definition {rest!r} (table PRIMARY KEY x{table_pk})")


def _rewrite(sql: str, table: str, new_name: str) -> str:
    """The same table, named ``new_name``, with ``id INTEGER [NOT NULL] PRIMARY KEY AUTOINCREMENT``."""
    head, body = _split_ddl(sql, table)
    column = _id_column(body, table)
    kind, _ = _shape(sql, table)
    rest = " ".join(column.group("rest").upper().split())
    if kind == "table_pk":
        definition = f"{column.group(1)} INTEGER {'NOT NULL ' if rest else ''}PRIMARY KEY AUTOINCREMENT"
        body = definition + body[column.end() :]
        body = _TABLE_PK.sub("", body, count=1)
    else:
        body = f"{column.group(1)} INTEGER {rest} AUTOINCREMENT" + body[column.end() :]
    head = re.sub(
        r"(\s*CREATE\s+TABLE\s+)(\"?)" + re.escape(table) + r"\2(\s*\()",
        lambda m: f"{m.group(1)}{_quote(new_name)}{m.group(3)}",
        head,
        count=1,
        flags=re.IGNORECASE,
    )
    return head + body


def _check_columns(xinfo: list[tuple], table: str) -> None:
    if not xinfo:
        raise UnsupportedShape(f"{table}: table is missing")
    if any(row[6] != 0 for row in xinfo):
        raise UnsupportedShape(f"{table}: hidden or generated columns")
    keys = [row for row in xinfo if row[5]]
    if len(keys) != 1 or keys[0][1] != "id" or (keys[0][2] or "").upper() != "INTEGER":
        raise UnsupportedShape(f"{table}: the primary key is not one INTEGER column named id")


def _fk_violations(conn) -> set[tuple]:
    return {tuple(row) for row in _rows(conn, "PRAGMA foreign_key_check")}


def _set_sequence(conn, table: str, value: int | None) -> int | None:
    current = _rows(conn, "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,))
    best = max([v for v in (value, current[0][0] if current else None) if v is not None], default=None)
    if best is None:
        return None
    if current:
        _run(conn, "UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (best, table))
    else:
        _run(conn, "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, best))
    return best


def _rebuild(conn, table: str, bound: Callable, fault: Fault) -> tuple[bool, int | None]:
    """Everything between ``BEGIN IMMEDIATE`` and ``COMMIT``. Raises on any refusal."""
    xinfo = _xinfo(conn, table)
    _check_columns(xinfo, table)
    (sql,) = _rows(conn, "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,))[0]
    _kind, already = _shape(sql, table)
    if already:
        fault("at_bound")
        return False, _set_sequence(conn, table, bound(conn))

    own = _rows(
        conn,
        "SELECT type, name, sql FROM sqlite_schema WHERE tbl_name = ? AND type IN ('index', 'trigger')"
        " AND sql IS NOT NULL ORDER BY type, name",
        (table,),
    )
    views = [
        name
        for (name, view_sql) in _rows(conn, "SELECT name, sql FROM sqlite_schema WHERE type = 'view'")
        if re.search(r"\b" + re.escape(table) + r"\b", view_sql or "", re.IGNORECASE)
    ]
    violations = _fk_violations(conn)
    staging = f"_c047_{table}_rebuild"
    if _rows(conn, "SELECT 1 FROM sqlite_schema WHERE name = ?", (staging,)):
        raise UnsupportedShape(f"{table}: a staging table {staging} already exists")

    fault("before_ddl")
    _run(conn, _rewrite(sql, table, staging))
    if [row[1:] for row in _xinfo(conn, staging)] != [row[1:] for row in xinfo]:
        raise UnsupportedShape(f"{table}: the rebuilt columns differ from the original")
    columns = ", ".join(_quote(row[1]) for row in xinfo)
    _run(conn, f"INSERT INTO {_quote(staging)} ({columns}) SELECT {columns} FROM {_quote(table)}")  # noqa: S608
    (copied,) = _rows(conn, f"SELECT count(*) FROM {_quote(staging)}")[0]  # noqa: S608
    (original,) = _rows(conn, f"SELECT count(*) FROM {_quote(table)}")[0]  # noqa: S608
    if copied != original:
        raise RuntimeError(f"{table}: copied {copied} of {original} rows")
    fault("after_copy")

    (legacy,) = _rows(conn, "PRAGMA legacy_alter_table")[0]
    _run(conn, f"DROP TABLE {_quote(table)}")
    fault("after_drop")
    _run(conn, "PRAGMA legacy_alter_table = ON")
    try:
        _run(conn, f"ALTER TABLE {_quote(staging)} RENAME TO {_quote(table)}")
    finally:
        _run(conn, f"PRAGMA legacy_alter_table = {int(legacy)}")
    for _type, _name, object_sql in own:
        _run(conn, object_sql)
    for name in views:
        _rows(conn, f"SELECT * FROM {_quote(name)} LIMIT 0")  # noqa: S608 — a view that no longer compiles stops here

    _run(conn, "DELETE FROM sqlite_sequence WHERE name IN (?, ?)", (staging, table))
    fault("at_bound")
    sequence = _set_sequence(conn, table, bound(conn))
    if _fk_violations(conn) != violations:
        raise RuntimeError(f"{table}: foreign-key violations changed during the rebuild")
    return True, sequence


def ensure_autoincrement(conn, table: str, bound: Callable, *, fault: Fault | None = None) -> Outcome:
    """Make ``table`` an AUTOINCREMENT table whose counter is at least ``bound(conn)``.

    ``conn`` must have no transaction open (a programming error otherwise). ``bound``
    is called inside the transaction. ``fault`` is a test hook called at named points.
    Raises :class:`SelfManagedFailure` (or :class:`UnsupportedShape`, wrapped) on failure.
    """
    fault = fault or (lambda _point: None)
    if _in_transaction(conn):
        raise SelfManagedFailure(f"{table}: the connection already has a transaction open", connection_safe=True)

    isolation = conn.isolation_level
    conn.isolation_level = None  # explicit BEGIN/COMMIT: the driver opens no transaction of its own
    (foreign_keys,) = _rows(conn, "PRAGMA foreign_keys")[0]
    safe = True
    failure: BaseException | None = None
    result: tuple[bool, int | None] = (False, None)
    try:
        _run(conn, "PRAGMA foreign_keys = OFF")
        _run(conn, "BEGIN IMMEDIATE")
        try:
            result = _rebuild(conn, table, bound, fault)
            fault("before_commit")
            _run(conn, "COMMIT")
        except BaseException as exc:
            failure = exc
            try:
                if _in_transaction(conn):
                    _run(conn, "ROLLBACK")
                if _rows(conn, "SELECT 1 FROM sqlite_schema WHERE name = ?", (f"_c047_{table}_rebuild",)):
                    safe = False
            except BaseException:  # noqa: BLE001 — an unproven rollback makes the connection unsafe
                safe = False
    finally:
        try:
            _run(conn, f"PRAGMA foreign_keys = {int(foreign_keys)}")
            if _rows(conn, "PRAGMA foreign_keys")[0][0] != foreign_keys or _in_transaction(conn):
                safe = False
        except BaseException:  # noqa: BLE001
            safe = False
        try:
            conn.isolation_level = isolation
        except BaseException:  # noqa: BLE001
            safe = False
    if failure is not None:
        if isinstance(failure, UnsupportedShape):
            raise SelfManagedFailure(str(failure), connection_safe=safe) from failure
        if isinstance(failure, Exception):
            raise SelfManagedFailure(f"{table}: rebuild failed: {failure}", connection_safe=safe) from failure
        raise failure  # cancellation and the like: the runner discards the connection
    rebuilt, sequence = result
    return Outcome(rebuilt=rebuilt, sequence=sequence, connection_safe=safe)
