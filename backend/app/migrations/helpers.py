"""Migration helpers - idempotent DDL operations for SQLite and PostgreSQL."""

import logging
import re
import sqlite3

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres

logger = logging.getLogger(__name__)


async def table_exists(conn, table: str) -> bool:
    """Check if a table exists in the database."""
    if is_postgres():
        result = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename=:name"),
            {"name": table},
        )
    else:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": table},
        )
    return result.scalar() is not None


async def column_exists(conn, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    if is_postgres():
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=:table AND column_name=:column"
            ),
            {"table": table, "column": column},
        )
        return result.scalar() is not None
    else:
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        return any(row[1] == column for row in result.fetchall())


_BOOL_DEFAULT_RE = re.compile(r"(\bBOOLEAN\b.*?\bDEFAULT\s+)([01])\b", re.IGNORECASE | re.DOTALL)


def _to_postgres_column_def(column_def: str) -> str:
    """Translate a SQLite-flavoured column definition into PostgreSQL syntax.

    Migrations are written SQLite-first and are frozen once released, so the
    dialect gap has to be closed here rather than in the 100+ call sites.

    Two translations:

    * ``BOOLEAN ... DEFAULT 0|1`` → ``DEFAULT false|true``. SQLite stores
      booleans as integers and accepts the integer default; PostgreSQL rejects
      it with ``column "x" is of type boolean but default expression is of type
      integer`` and aborts the whole migration chain. This previously read
      ``.replace("BOOLEAN", "BOOLEAN")`` — a no-op that silently translated
      nothing, which is why every fresh PostgreSQL install died here.
    * ``INTEGER PRIMARY KEY`` → ``SERIAL PRIMARY KEY`` (SQLite's implicit
      rowid alias has no PostgreSQL equivalent).

    Only the default literal is rewritten; the column name, any CHECK and the
    rest of the definition are left exactly as written.
    """
    out = _BOOL_DEFAULT_RE.sub(lambda m: m.group(1) + ("true" if m.group(2) == "1" else "false"), column_def)
    return out.replace("INTEGER PRIMARY KEY", "SERIAL PRIMARY KEY")


async def add_column(conn, table: str, column_def: str) -> bool:
    """Add a column if it doesn't exist. Returns True if added."""
    col_name = column_def.strip().split()[0]
    if await column_exists(conn, table, col_name):
        return False
    if is_postgres():
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {_to_postgres_column_def(column_def)}"))
    else:
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_def}"))
    return True


async def recreate_table(conn, table: str, new_ddl: str, columns_to_copy: str) -> None:
    """Drop columns by recreating a table (SQLite) or using ALTER TABLE (PostgreSQL).

    On SQLite: copy data to temp table with new schema, drop old, rename.
    On PostgreSQL: use ALTER TABLE DROP COLUMN for columns not in the new schema.

    Args:
        conn: SQLAlchemy async connection (inside engine.begin())
        table: Table name
        new_ddl: Full CREATE TABLE statement for the new schema
        columns_to_copy: Comma-separated column names to preserve
    """
    if is_postgres():
        # PostgreSQL supports ALTER TABLE DROP COLUMN natively
        current_cols = await get_table_columns(conn, table)
        keep_cols = {c.strip() for c in columns_to_copy.split(",")}
        for col in current_cols:
            if col not in keep_cols:
                await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {col}"))
    else:
        # SQLite: copy-drop-rename workaround
        tmp = f"_mig_tmp_{table}"
        # Only the columns the SOURCE actually has. A migration's copy list is
        # frozen with it, so a LATER migration that drops one of those columns
        # breaks a FRESH install: the table is built from today's model and the
        # whole chain then runs from the start, reaching this one with a column
        # that was never there. Frozen migrations cannot be edited, which leaves
        # this — and copying a column the source lacks was never right anyway,
        # since there is nothing in it to keep.
        present = set(await get_table_columns(conn, table))
        wanted = [c.strip() for c in columns_to_copy.split(",") if c.strip()]
        copied = [c for c in wanted if c in present]
        missing = [c for c in wanted if c not in present]
        if missing:
            logger.info("recreate_table %s: skipping column(s) the table no longer has: %s", table, ", ".join(missing))
        copy_list = ", ".join(copied)
        await conn.execute(text(f"DROP TABLE IF EXISTS {tmp}"))
        await conn.execute(text(new_ddl.replace(f"CREATE TABLE {table}", f"CREATE TABLE {tmp}")))
        await conn.execute(text(f"INSERT INTO {tmp} ({copy_list}) SELECT {copy_list} FROM {table}"))
        await conn.execute(text(f"DROP TABLE {table}"))
        await conn.execute(text(f"ALTER TABLE {tmp} RENAME TO {table}"))


async def get_table_columns(conn, table: str) -> list[str]:
    """Get list of column names for a table."""
    if is_postgres():
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=:name ORDER BY ordinal_position"
            ),
            {"name": table},
        )
        return [row[0] for row in result.fetchall()]
    else:
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        return [row[1] for row in result.fetchall()]


# SQLite gained ALTER TABLE ... DROP COLUMN in 3.35.0 (2021). Below that the only
# route is rewriting the table from a hand-written CREATE TABLE — which for a
# core table is how NOT NULL, DEFAULT, FK and UNIQUE get silently lost, with no
# way to tell afterwards and no way to repair it.
_SQLITE_DROP_COLUMN_SINCE = (3, 35, 0)


async def drop_column(conn, table: str, column: str) -> bool:
    """Remove a column. True when it is gone afterwards.

    Idempotent: a column that is not there is already dropped, and ``DEBUG=true``
    re-runs the newest migration on every start.

    On an SQLite too old for this, the column is LEFT IN PLACE and a warning says
    so. That is deliberate — a vestigial column nothing reads is harmless, while
    a rewritten core table with a lost constraint is not repairable.
    """
    if column not in await get_table_columns(conn, table):
        return True

    if is_postgres():
        await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}"))
        return True

    version = tuple(int(part) for part in sqlite3.sqlite_version.split("."))
    if version < _SQLITE_DROP_COLUMN_SINCE:
        logger.warning(
            "SQLite %s cannot drop a column; leaving %s.%s in place, unused. "
            "Nothing reads it — upgrade SQLite to remove it.",
            sqlite3.sqlite_version,
            table,
            column,
        )
        return False

    await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    return True
