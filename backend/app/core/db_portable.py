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

    # Phase 1: Drop and recreate the schema, then strip foreign keys IN THE
    # DATABASE before loading data.
    #
    # This used to remove the constraint objects from ``metadata`` and rely on
    # ``create_all`` emitting FK-free DDL. That silently fails for the cyclic
    # group (auto_queue_items ↔ library_files ↔ library_folders ↔ print_archives
    # ↔ print_queue): SQLAlchemy cannot inline a cycle, so it emits those FKs as
    # separate ALTER TABLE statements built from the column-level ``ForeignKey``
    # objects, which the metadata surgery never touched. Measured on a real
    # PostgreSQL: 99 constraints before, 20 still standing after the strip — and
    # the first insert into library_files then died on a folder_id violation,
    # aborting the whole migration.
    #
    # Dropping them from the catalogue instead is immune to how SQLAlchemy
    # chooses to emit DDL, and lets the rows land in any order.
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

        # Now strip the foreign keys from the catalogue itself, remembering each
        # definition verbatim so Phase 3 can put it back exactly as PostgreSQL
        # rendered it. ``pg_get_constraintdef`` gives us the full
        # ``FOREIGN KEY (...) REFERENCES ... ON DELETE ...`` clause.
        saved_db_fks = []
        if is_postgres():
            rows = (
                await conn.execute(
                    text(
                        "SELECT c.conrelid::regclass::text AS child, c.conname, "
                        "       pg_get_constraintdef(c.oid) AS cdef, "
                        "       c.confrelid::regclass::text AS parent, "
                        "       (SELECT array_agg(a.attname ORDER BY u.ord) "
                        "          FROM unnest(c.conkey) WITH ORDINALITY u(attnum, ord) "
                        "          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = u.attnum) AS ccols, "
                        "       (SELECT array_agg(a.attname ORDER BY u.ord) "
                        "          FROM unnest(c.confkey) WITH ORDINALITY u(attnum, ord) "
                        "          JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = u.attnum) AS pcols "
                        "FROM pg_constraint c "
                        "WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace"
                    )
                )
            ).all()
            saved_db_fks = [(r[0], r[1], r[2], r[3], list(r[4]), list(r[5])) for r in rows]
            for tbl, conname, *_ in saved_db_fks:
                await conn.execute(text(f'ALTER TABLE {tbl} DROP CONSTRAINT "{conname}"'))
            logger.info("Dropped %d foreign keys for the duration of the import", len(saved_db_fks))

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

    # Phase 3: Put the foreign keys back, exactly as they were.
    #
    # A failure here is NOT cosmetic: it means the imported rows contain
    # references the constraint forbids. SQLite does not enforce foreign keys
    # unless ``PRAGMA foreign_keys=ON`` is set per connection, so a legacy
    # database can genuinely carry orphans that PostgreSQL will refuse. Log each
    # one by name and raise — a database silently missing constraints is a worse
    # outcome than a migration that stops and says why.
    if is_postgres():
        failed, purged = [], []
        for tbl, conname, cdef, parent, ccols, pcols in saved_db_fks:
            for attempt in (1, 2):
                try:
                    async with engine.begin() as fk_conn:
                        await fk_conn.execute(text(f'ALTER TABLE {tbl} ADD CONSTRAINT "{conname}" {cdef}'))
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 2:
                        failed.append(f"{tbl}.{conname}: {e}")
                        logger.error("Could not restore FK %s on %s: %s", conname, tbl, e)
                        break
                    # First failure means the source carries rows pointing at a
                    # parent that no longer exists. SQLite does not enforce
                    # foreign keys unless PRAGMA foreign_keys=ON, so a long-lived
                    # database accumulates these silently — a plan item for a
                    # deleted project, say, which no screen can reach anyway.
                    # Delete exactly those rows (NULL references are legal and
                    # left alone), say how many and from where, then retry once.
                    # Refusing to migrate over unreachable junk would be worse;
                    # dropping it without a word would be worse still.
                    on = " AND ".join(f"p.{pc} = c.{cc}" for cc, pc in zip(ccols, pcols, strict=True))
                    notnull = " AND ".join(f"c.{cc} IS NOT NULL" for cc in ccols)
                    async with engine.begin() as fk_conn:
                        res = await fk_conn.execute(
                            text(
                                f"DELETE FROM {tbl} c WHERE {notnull} "  # noqa: S608
                                f"AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE {on})"
                            )
                        )
                        if res.rowcount:
                            purged.append(f"{tbl}: {res.rowcount} row(s) referencing missing {parent}")
                            logger.warning(
                                "Purged %d orphaned row(s) from %s (dangling %s reference)",
                                res.rowcount,
                                tbl,
                                parent,
                            )
        if failed:
            raise RuntimeError(
                f"{len(failed)} foreign key(s) could not be restored after import: {'; '.join(failed[:5])}"
            )
        logger.info("Restored %d foreign keys", len(saved_db_fks))
        if purged:
            logger.warning("Import dropped orphaned rows the source database was carrying: %s", "; ".join(purged))

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
