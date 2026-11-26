"""What every table whose numbers are never handed out again shares (P-ID, N07 spec §B).

Such a table has two things to ask before its counter is moved. The carriers — the
columns a current reader could still pick a number up from — give the bound each
table's own registry computes. The allocator itself — SQLite's ``MAX(id)`` and its
``sqlite_sequence`` row, a PostgreSQL sequence's next value — is asked separately:
a counter already at the ceiling leaves no number even when no carrier remembers one
(PID-S25, N07-S15). Both checks live here once and take the table by name.

Domain: an id is 1..2³¹ − 1, PostgreSQL's ``integer`` and inside JavaScript's exact
integers. A value outside it is not reserved and does not raise a bound.

SQLite keeps whatever was written, so a value's own class decides (``typeof``), not
the declared type: a ``real`` or ``text`` value counts only when ``int()`` takes it
without loss.
"""

from __future__ import annotations

import math

MAX_ID = 2**31 - 1


def in_domain(value: int) -> bool:
    return 1 <= value <= MAX_ID


def rows(conn, sql: str, params=()) -> list[tuple]:
    """Rows through any DB-API connection (sqlite3 or the aiosqlite adapter)."""
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params)
        return list(cursor.fetchall())
    finally:
        cursor.close()


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def lossless_int(value) -> int | None:
    """A SQLite ``real`` or ``text`` value in an integer column that ``int()`` takes without loss."""
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def sqlite_int_values(conn, table: str, column: str) -> list[int]:
    """The in-domain numbers an integer column holds: its largest real integer, plus every
    ``real``/``text`` value that is a lossless integer. Reads only."""
    t, c = quote(table), quote(column)
    # One scan: the largest real integer in the domain, and whether any real/text value needs a closer look.
    top, others = rows(
        conn,
        f"SELECT MAX(CASE WHEN typeof({c}) = 'integer' AND {c} BETWEEN 1 AND ? THEN {c} END),"  # noqa: S608
        f" SUM(CASE WHEN typeof({c}) IN ('real', 'text') THEN 1 ELSE 0 END) FROM {t}",
        (MAX_ID,),
    )[0]
    values = [top] if top is not None else []
    if others:
        found = rows(conn, f"SELECT DISTINCT {c} FROM {t} WHERE typeof({c}) IN ('real', 'text')")  # noqa: S608
        values += [v for v in (lossless_int(raw) for (raw,) in found) if v is not None and in_domain(v)]
    return values


def sqlite_allocator(conn, table: str) -> int | None:
    """The highest number SQLite itself would continue from for ``table``: ``MAX(id)`` and the
    ``sqlite_sequence`` counter, domain or not. Reads only."""
    found = []
    if rows(conn, "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)):
        (top,) = rows(conn, f"SELECT MAX(id) FROM {quote(table)} WHERE typeof(id) = 'integer'")[0]  # noqa: S608
        found.append(top)
    if rows(conn, "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'sqlite_sequence'"):
        found += [seq for (seq,) in rows(conn, "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,))]
    numbers = [v for v in found if isinstance(v, int) and not isinstance(v, bool)]
    return max(numbers) if numbers else None


def sqlite_applied_versions(conn) -> set[int]:
    """The ``_migrations`` versions of a SQLite database; none when it has no such table."""
    if not rows(conn, "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = '_migrations'"):
        return set()
    return {row[0] for row in rows(conn, "SELECT version FROM _migrations")}


def require_next_number(table: str, noun: str, *highest: int | None, error: type[Exception] = RuntimeError) -> None:
    """Stop when no number is left at or below ``MAX_ID`` after ``highest`` (carriers and allocator)."""
    numbers = [v for v in highest if v is not None]
    if numbers and max(numbers) >= MAX_ID:
        raise error(
            f"{table}: the id counter is exhausted — {max(numbers)} is already remembered and no {noun}"
            f" number is left up to {MAX_ID}"
        )


def require_next_postgres_number(state, bound: int | None, table: str) -> None:
    """Stop when the sequence's next number, or the first one above the bound, lies past the
    ceiling ``min(seqmax, 2³¹ − 1)`` — asked of the allocator itself, with or without a carrier."""
    from backend.app.core import pg_sequences

    ceiling = min(state.maximum, MAX_ID)
    if state.next_value > ceiling or (bound is not None and bound >= ceiling):
        raise pg_sequences.SequenceError(
            f"{table}: the id sequence is exhausted (bound {bound}, next {state.next_value}, ceiling {ceiling})"
        )
