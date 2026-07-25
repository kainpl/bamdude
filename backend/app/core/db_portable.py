"""Portable database operations - cross-dialect backup, restore, and auto-migration.

Backups are always in portable SQLite format regardless of database backend.
Restore can import SQLite backups into both SQLite and PostgreSQL.
Auto-migration transfers data from local SQLite to PostgreSQL on first PG start.
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Marker for "this NOT NULL column has a server-side default, substitute the
# current timestamp". The import path spells the same idea as the string
# ``"__now__"``; a sentinel object avoids colliding with a real stored value.
_NOW_SENTINEL = object()


def _is_datetime_column(col) -> bool:
    """Whether the column stores a date/time, so a ``func.now()`` server-default
    can be substituted with a Python ``datetime``."""
    type_name = str(col.type).upper()
    return "TIMESTAMP" in type_name or "DATETIME" in type_name or type_name == "DATE"


async def dump_to_sqlite(engine, metadata, output_path: Path) -> None:
    """Export current database (any backend) to a portable SQLite file.

    For SQLite backend: checkpoint WAL and copy the file directly.
    For PostgreSQL: read all tables via ORM and write to a new SQLite file.
    """
    from backend.app.core.db_dialect import is_sqlite

    if is_sqlite():
        import shutil

        from backend.app.core.config import settings

        db_path = Path(settings.database_url.replace("sqlite+aiosqlite:///", ""))
        # Checkpoint WAL to ensure all data is in main db file
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        shutil.copy2(db_path, output_path)
    else:
        await _export_pg_to_sqlite(engine, metadata, output_path)


async def _export_pg_to_sqlite(engine, metadata, output_path: Path) -> None:
    """Export PostgreSQL data to a portable SQLite file.

    The schema is emitted by ``Base.metadata.create_all()`` against a real
    SQLite engine, so the portable file gets **exactly the DDL a native SQLite
    install has**: NOT NULL, DEFAULT (``func.now()`` → ``CURRENT_TIMESTAMP``),
    foreign keys, unique constraints, CHECKs, indexes and ``LargeBinary`` →
    ``BLOB``. This replaced a hand-rolled ``CREATE TABLE`` loop that emitted
    only column name + coarse type + primary key (upstream #2526).

    Why that mattered: restoring one of these backups onto a SQLite install
    page-copies the schema straight onto the live database, and the
    post-restore ``init_db()`` cannot repair it — ``create_all`` is
    ``CREATE TABLE IF NOT EXISTS``. So every ``server_default`` column (we have
    121 of them, 91 being the ``created_at``/``updated_at`` ``func.now()``
    pattern) lost its DEFAULT: SQLAlchemy omits server-default columns from the
    INSERT, the database had no DEFAULT to supply, NULL was written, and the
    next read failed Pydantic validation with a 500 on whole list endpoints.

    Known gap, unchanged here: the export walks ``metadata.sorted_tables``, so
    the raw-SQL ``_migrations`` table and the SQLite-only ``archive_fts`` FTS5
    virtual table + triggers are still absent from a PG-origin portable file.
    """
    import json

    from sqlalchemy import create_engine as create_sync_engine

    # Full-fidelity DDL, identical to what a native SQLite install gets.
    schema_engine = create_sync_engine(f"sqlite:///{output_path}")
    try:
        metadata.create_all(schema_engine)
    finally:
        schema_engine.dispose()

    dst = sqlite3.connect(str(output_path))
    # Our FK graph has cycles (auto_queue_items / library_files / library_folders
    # / print_archives / print_queue), so `sorted_tables` insert order is not
    # safe under enforcement. sqlite3 defaults this to OFF; be explicit now that
    # the portable file actually carries foreign keys.
    dst.execute("PRAGMA foreign_keys = OFF")

    # Export data
    async with engine.connect() as conn:
        for table in metadata.sorted_tables:
            result = await conn.execute(table.select())
            rows = result.fetchall()
            if not rows:
                continue
            columns = list(result.keys())
            placeholders = ", ".join(["?"] * len(columns))
            col_list = ", ".join(columns)
            insert_sql = f"INSERT INTO {table.name} ({col_list}) VALUES ({placeholders})"  # noqa: S608

            # Now that NOT NULL is enforced in the portable file, a source row
            # holding NULL in a model-NOT-NULL column would abort the whole
            # backup. That happens where a migration added a column nullable
            # while the model declares it NOT NULL. Fill from the column's own
            # default, mirroring what `import_sqlite_to_postgres` already does
            # on the way in.
            not_null_defaults: dict[str, object] = {}
            for col in table.columns:
                if col.name not in columns or col.nullable:
                    continue
                if col.default is not None:
                    default = col.default.arg
                    not_null_defaults[col.name] = default(None) if callable(default) else default
                elif col.server_default is not None and _is_datetime_column(col):
                    # Only datetime server-defaults are substitutable — they are
                    # all `func.now()`. A boolean/integer server_default is an
                    # SQL expression we must NOT guess at: filling a timestamp
                    # into an integer column would be worse than the NULL, so
                    # leave it and let the IntegrityError below name the table.
                    not_null_defaults[col.name] = _NOW_SENTINEL

            now = datetime.now()  # noqa: DTZ005

            def _serialize_row(row, cols=columns, nn=not_null_defaults, _n=now):
                values = []
                for name, v in zip(cols, row, strict=True):
                    if v is None and name in nn:
                        v = _n if nn[name] is _NOW_SENTINEL else nn[name]
                    values.append(json.dumps(v) if isinstance(v, (list, dict)) else v)
                return tuple(values)

            try:
                dst.executemany(insert_sql, [_serialize_row(row) for row in rows])
            except sqlite3.IntegrityError:
                logger.error(
                    "Portable export: table %s violates the model schema; backup aborted",
                    table.name,
                )
                dst.close()
                raise

    dst.commit()
    dst.close()
    logger.info("PostgreSQL exported to portable SQLite: %s", output_path)


async def import_sqlite_to_postgres(engine, metadata, sqlite_path: Path) -> int:
    """Import data from a SQLite file into the current PostgreSQL database.

    Used for cross-database restore and auto-migration.
    Drops and recreates tables without FKs, imports data, then restores FKs.

    Returns number of tables imported.
    """
    src = sqlite3.connect(str(sqlite_path))
    src.row_factory = sqlite3.Row

    # Get source tables (skip internal/FTS)
    cursor = src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'archive_fts%' "
        "AND name != '_migrations'"
    )
    src_tables = {row["name"] for row in cursor.fetchall()}
    pg_tables = set(metadata.tables.keys())
    tables_to_import = src_tables & pg_tables
    sorted_tables = [t.name for t in metadata.sorted_tables if t.name in tables_to_import]

    # Phase 1: Drop and recreate tables WITHOUT foreign keys
    saved_fks = {}
    for table in metadata.sorted_tables:
        fks = list(table.foreign_key_constraints)
        if fks:
            saved_fks[table.name] = fks
            for fk in fks:
                table.constraints.remove(fk)

    async with engine.begin() as conn:
        # On PostgreSQL, plain metadata.drop_all only enumerates ORM-defined tables
        # and emits non-CASCADE DROP TABLE. Orphan tables left over from removed
        # features (e.g. legacy spoolman_* whose FKs still reference printers) then
        # block the drop with DependentObjectsStillExistError, aborting the whole
        # restore. Drop every public-schema table with CASCADE first so the orphans
        # and their constraints come down alongside the ORM ones; restricted to
        # schemaname='public' so a shared Postgres instance with non-BamDude data
        # in other schemas isn't affected. SQLite is unaffected (no orphan-FK risk).
        from backend.app.core.db_dialect import is_postgres

        if is_postgres():
            # Cap how long DROP TABLE will wait for AccessExclusiveLock so any
            # residual concurrent writer (a per-printer MQTT client writing
            # reactively, a background loop that woke on its cadence) surfaces a
            # fast `lock_timeout` error instead of blocking the restore for the
            # default cadence or producing an AB/BA deadlock. SET LOCAL scopes
            # to this transaction only; outside this restore path the global
            # default (no timeout) applies. Pairs with the background-service
            # pause in routes/settings.py::restore_backup (#1... PG deadlock).
            await conn.execute(text("SET LOCAL lock_timeout = '10s'"))
            await conn.execute(
                text(
                    "DO $$ DECLARE r RECORD; "
                    "BEGIN FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP "
                    "EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE'; "
                    "END LOOP; END $$;"
                )
            )
        else:
            await conn.run_sync(metadata.drop_all)
        await conn.run_sync(metadata.create_all)

    # Restore FK definitions in metadata
    for table_name, fks in saved_fks.items():
        table_obj = metadata.tables[table_name]
        for fk in fks:
            table_obj.constraints.add(fk)

    # Phase 2: Import data
    imported = 0
    async with engine.begin() as conn:
        for table_name in sorted_tables:
            rows = src.execute(f"SELECT * FROM {table_name}").fetchall()  # noqa: S608
            if not rows:
                continue

            # Filter to columns that exist in PG table
            src_columns = rows[0].keys()
            pg_table = metadata.tables.get(table_name)
            if pg_table is None:
                continue
            pg_columns = {c.name for c in pg_table.columns}
            columns = [c for c in src_columns if c in pg_columns]
            if not columns:
                continue

            col_list = ", ".join(columns)
            param_list = ", ".join(f":{c}" for c in columns)
            insert_sql = text(
                f"INSERT INTO {table_name} ({col_list}) VALUES ({param_list}) ON CONFLICT DO NOTHING"  # noqa: S608
            )

            # Identify type conversions needed
            bool_columns = set()
            datetime_columns = set()
            not_null_defaults: dict[str, object] = {}

            for col in pg_table.columns:
                if col.name not in columns:
                    continue
                col_type = str(col.type).upper()
                if col_type == "BOOLEAN":
                    bool_columns.add(col.name)
                elif "TIMESTAMP" in col_type or col_type == "DATETIME":
                    datetime_columns.add(col.name)
                if not col.nullable and col.default is not None:
                    default = col.default.arg
                    if callable(default):
                        default = default(None)
                    not_null_defaults[col.name] = default
                elif not col.nullable and col.server_default is not None:
                    if col.name in datetime_columns:
                        not_null_defaults[col.name] = "__now__"

            now = datetime.now()  # noqa: DTZ005

            def _convert_row(row, cols=columns, bools=bool_columns, dts=datetime_columns, nn=not_null_defaults, _n=now):
                result = {}
                for c in cols:
                    val = row[c]
                    if val is None and c in nn:
                        val = _n if nn[c] == "__now__" else nn[c]
                    if val is not None:
                        if c in bools:
                            val = bool(val)
                        elif c in dts and isinstance(val, str):
                            try:
                                val = datetime.fromisoformat(val)  # noqa: DTZ011
                            except ValueError:
                                pass
                    result[c] = val
                return result

            batch = [_convert_row(row) for row in rows]
            await conn.execute(insert_sql, batch)
            imported += 1
            logger.info("Imported %d rows into %s", len(batch), table_name)

        # Reset sequences to max(id) + 1
        for table_name in sorted_tables:
            try:
                async with conn.begin_nested():
                    result = await conn.execute(text(f"SELECT MAX(id) FROM {table_name}"))  # noqa: S608
                    max_id = result.scalar()
                    if max_id is not None:
                        await conn.execute(text(f"SELECT setval('{table_name}_id_seq', {max_id})"))  # noqa: S608
            except Exception:
                pass  # Table may not have an id column or sequence

    src.close()

    # Phase 3: Restore FK constraints
    from sqlalchemy.schema import AddConstraint

    for table in metadata.sorted_tables:
        for fk in table.foreign_key_constraints:
            try:
                async with engine.begin() as fk_conn:
                    await fk_conn.execute(AddConstraint(fk))
            except Exception as e:
                logger.warning("Could not add FK %s.%s: %s", table.name, fk.name, e)

    logger.info("Cross-database import complete: %d tables imported", imported)
    return imported


async def auto_migrate_sqlite_to_pg(engine, metadata) -> bool:
    """Auto-migrate local SQLite database to PostgreSQL on first PG start.

    Called during startup when:
    - DATABASE_URL points to PostgreSQL
    - PostgreSQL is empty (no data)
    - Local bamdude.db exists

    Returns True if migration was performed.
    """
    from backend.app.core.config import settings
    from backend.app.core.db_dialect import is_postgres

    if not is_postgres():
        return False

    # Check if PG already has data
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text("SELECT COUNT(*) FROM printers"))
            if (result.scalar() or 0) > 0:
                return False  # PG already populated
    except Exception:
        return False  # Table doesn't exist yet or other error

    # Look for local SQLite database
    sqlite_path = Path(settings.data_dir) / "bamdude.db"
    if not sqlite_path.exists():
        # Also check for legacy names
        for name in ("bambuddy.db", "bambutrack.db"):
            alt = Path(settings.data_dir) / name
            if alt.exists():
                sqlite_path = alt
                break
        else:
            return False  # No local SQLite to migrate

    logger.info("Found local SQLite database: %s - migrating to PostgreSQL...", sqlite_path)

    try:
        imported = await import_sqlite_to_postgres(engine, metadata, sqlite_path)

        # Rename SQLite to .migrated to prevent re-import
        migrated_path = sqlite_path.with_suffix(".db.migrated")
        if migrated_path.exists():
            migrated_path.unlink()
        sqlite_path.rename(migrated_path)
        # Clean up WAL/SHM
        for suffix in ("-wal", "-shm"):
            wal = sqlite_path.parent / (sqlite_path.name + suffix)
            if wal.exists():
                wal.unlink()

        logger.info(
            "SQLite → PostgreSQL migration complete (%d tables). Original renamed to %s", imported, migrated_path.name
        )
        return True

    except Exception as e:
        logger.error("SQLite → PostgreSQL migration failed: %s", e)
        return False
