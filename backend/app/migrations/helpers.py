"""Migration helpers - idempotent DDL operations for SQLite and PostgreSQL."""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres


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


async def add_column(conn, table: str, column_def: str) -> bool:
    """Add a column if it doesn't exist. Returns True if added."""
    col_name = column_def.strip().split()[0]
    if await column_exists(conn, table, col_name):
        return False
    if is_postgres():
        # Convert SQLite-style defaults to PostgreSQL syntax
        pg_def = column_def.replace("BOOLEAN", "BOOLEAN").replace("INTEGER PRIMARY KEY", "SERIAL PRIMARY KEY")
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {pg_def}"))
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
        await conn.execute(text(f"DROP TABLE IF EXISTS {tmp}"))
        await conn.execute(text(new_ddl.replace(f"CREATE TABLE {table}", f"CREATE TABLE {tmp}")))
        await conn.execute(text(f"INSERT INTO {tmp} ({columns_to_copy}) SELECT {columns_to_copy} FROM {table}"))
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
