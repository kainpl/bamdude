"""SQLite migration helpers — idempotent DDL operations."""

from sqlalchemy import text


async def table_exists(conn, table: str) -> bool:
    """Check if a table exists in the database."""
    result = await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table},
    )
    return result.scalar() is not None


async def column_exists(conn, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    return any(row[1] == column for row in result.fetchall())


async def add_column(conn, table: str, column_def: str) -> bool:
    """Add a column if it doesn't exist. Returns True if added."""
    col_name = column_def.strip().split()[0]
    if await column_exists(conn, table, col_name):
        return False
    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_def}"))
    return True


async def recreate_table(conn, table: str, new_ddl: str, columns_to_copy: str) -> None:
    """SQLite DROP COLUMN workaround via table recreation.

    Args:
        conn: SQLAlchemy async connection (inside engine.begin())
        table: Table name
        new_ddl: Full CREATE TABLE statement for the new schema
        columns_to_copy: Comma-separated column names to preserve
    """
    tmp = f"_mig_tmp_{table}"
    await conn.execute(text(f"DROP TABLE IF EXISTS {tmp}"))
    # Create temp table with new schema
    await conn.execute(text(new_ddl.replace(f"CREATE TABLE {table}", f"CREATE TABLE {tmp}")))
    # Copy data
    await conn.execute(text(f"INSERT INTO {tmp} ({columns_to_copy}) SELECT {columns_to_copy} FROM {table}"))
    # Swap
    await conn.execute(text(f"DROP TABLE {table}"))
    await conn.execute(text(f"ALTER TABLE {tmp} RENAME TO {table}"))


async def get_table_columns(conn, table: str) -> list[str]:
    """Get list of column names for a table."""
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    return [row[1] for row in result.fetchall()]
