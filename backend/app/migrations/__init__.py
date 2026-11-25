"""BamDude migration system - auto-discovered versioned migrations.

Runner logic:
1. create_all() - creates/updates tables from models
2. Ensure _migrations table
3. Run all pending migrations sequentially (m000, m001, m002...)

m000 is inert since 0.5.6 (the Bambuddy import was removed); the version-0 record is kept for the
bootstrap, and the per-boot notice about a Bambuddy file lives in run_all_migrations, not in m000.
m001 is baseline - FTS5 + seeds for BamDude 3.0.1.
m002+ are incremental upgrades between BamDude versions.
"""

import importlib
import logging
import pkgutil
from pathlib import Path

from sqlalchemy import inspect, text

logger = logging.getLogger(__name__)

# One definition, two callers: the startup check below and the PostgreSQL
# auto-migrate in core/db_portable.py, which refuses the same file. Keeping the
# sentence in one place is what stops the two paths telling a user different
# stories about what happened to their database.
FOREIGN_BAMBUDDY_NOTICE = (
    "Found a Bambuddy database at %s. Importing Bambuddy data was removed in 0.5.6 — "
    "BamDude and Bambuddy have diverged too far for a one-time import; the file is left "
    "untouched. See https://docs.bamdude.top/getting-started/upgrading/"
)


async def _warn_if_foreign_bambuddy_file(data_dir) -> None:
    """Name an upstream Bambuddy database sitting in the data directory.

    Deliberately a startup check rather than part of m000. A migration is
    recorded in ``_migrations`` and therefore runs exactly once: a notice living
    in m000 fired on the first boot of a fresh install and stayed silent on every
    boot after it, while the file it describes sits in the data directory
    indefinitely. Nothing renames or removes that file any more — the ``.bak``
    rename went with the importer — so the only way the notice can keep being
    true is to re-check on every start.

    Silent for BamDude's own 3.0.1-era ``bambuddy.db``: ``run_all_migrations``
    has already renamed that one to ``bamdude.db`` above, and calling it foreign
    would tell a BamDude user their own database was ignored.
    """
    path = _find_legacy_database(data_dir)
    if path and not await _is_bamdude_301(path):
        logger.warning(FOREIGN_BAMBUDDY_NOTICE, path)


def _discover_migrations() -> list[dict]:
    """Find all m*.py migration files, sorted by version."""
    migrations = []
    package = importlib.import_module("backend.app.migrations")
    for _importer, modname, _ispkg in pkgutil.iter_modules(package.__path__):
        if modname.startswith("m") and len(modname) > 3 and modname[1:4].isdigit():
            mod = importlib.import_module(f"backend.app.migrations.{modname}")
            if hasattr(mod, "version") and hasattr(mod, "name"):
                migrations.append(
                    {
                        "version": mod.version,
                        "name": mod.name,
                        "module": mod,
                    }
                )
    migrations.sort(key=lambda m: m["version"])
    return migrations


async def _ensure_migrations_table(engine) -> None:
    """Create _migrations table if it doesn't exist."""
    from backend.app.core.db_dialect import is_postgres

    async with engine.begin() as conn:
        if is_postgres():
            await conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS _migrations (
                    id SERIAL PRIMARY KEY,
                    version INTEGER NOT NULL UNIQUE,
                    name VARCHAR(100) NOT NULL,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            )
        else:
            await conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS _migrations (
                    id INTEGER PRIMARY KEY,
                    version INTEGER NOT NULL UNIQUE,
                    name VARCHAR(100) NOT NULL,
                    applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            )


async def _get_applied_versions(engine) -> set[int]:
    """Get set of already-applied migration versions."""
    async with engine.begin() as conn:
        result = await conn.execute(text("SELECT version FROM _migrations"))
        return {row[0] for row in result.fetchall()}


async def _record_migration(engine, version: int, name: str) -> None:
    """Record a migration as applied."""
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO _migrations (version, name) VALUES (:version, :name)"),
            {"version": version, "name": name},
        )


async def _bootstrap_existing(engine) -> None:
    """For existing BamDude 3.0.1 installs that predate the migration system.

    If _migrations table is empty, mark m000 and m001 as applied
    (schema is already at 3.0.1 level).
    """
    applied = await _get_applied_versions(engine)
    if not applied:
        # Mark m000 (import) and m001 (baseline) as already done
        await _record_migration(engine, 0, "bambuddy_to_bamdude_301")
        await _record_migration(engine, 1, "bamdude_baseline")
        logger.info("Bootstrapped existing BamDude 3.0.1 install (m000+m001 marked as applied)")


def _owns_sqlite_transaction(mod) -> bool:
    """Whether the migration, on SQLite, owns its transaction and ``PRAGMA foreign_keys`` itself.

    Such a module exposes ``upgrade_sqlite(dbapi_connection)`` (C047, spec §A). SQLite ignores
    the pragma inside a transaction, so a rebuild that must run with foreign keys off cannot
    live inside the runner's ``engine.begin()``; its PostgreSQL arm still goes through ``upgrade``.
    """
    return bool(getattr(mod, "sqlite_owns_transaction", False))


async def _run_sqlite_self_managed(engine, mod) -> None:
    """Run ``mod.upgrade_sqlite`` on one pooled connection with no transaction open on it.

    The SAME entry serves the pending run and the DEBUG replay. Nothing is executed through
    SQLAlchemy before the hand-over, so neither SQLAlchemy nor the driver has begun a
    transaction, and the runner does not touch ``PRAGMA foreign_keys`` here. A connection
    whose rollback or pragma restore the helper could not prove — or that an interruption
    left in an unknown state — is invalidated, never returned to the pool.
    """
    from backend.app.migrations._sqlite_autoincrement import SelfManagedFailure

    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        dbapi = raw.dbapi_connection
        try:
            outcome = await conn.run_sync(lambda _sync: mod.upgrade_sqlite(dbapi))
        except SelfManagedFailure as exc:
            if not exc.connection_safe:
                await conn.invalidate()
            raise
        except BaseException:
            await conn.invalidate()
            raise
        if not outcome.connection_safe:
            logger.warning("Migration %s left its connection in an unproven state; discarding it", mod.version)
            await conn.invalidate()


async def _run_pending(engine, session_factory) -> None:
    """Discover and run all pending migrations sequentially."""
    from backend.app.core.config import settings as app_settings
    from backend.app.core.db_dialect import is_sqlite

    applied = await _get_applied_versions(engine)
    migrations = _discover_migrations()

    # Dev mode: re-run the latest migration every time (for iterating on schema/seeds)
    if app_settings.debug and migrations and applied:
        latest = max(m["version"] for m in migrations)
        if latest in applied:
            try:
                head = next(m["module"] for m in migrations if m["version"] == latest)
                if is_sqlite() and _owns_sqlite_transaction(head):
                    await _run_sqlite_self_managed(engine, head)
                elif hasattr(head, "replay"):
                    async with engine.begin() as conn:
                        await head.replay(conn)
                else:
                    async with engine.begin() as conn:
                        await conn.execute(text("DELETE FROM _migrations WHERE version = :v"), {"v": latest})
                    applied.discard(latest)
                logger.info("Dev mode: re-running migration %d", latest)
            except Exception as exc:
                logger.error("Migration %d failed during DEBUG replay (%s)", latest, type(exc).__name__)
                raise

    pending = [m for m in migrations if m["version"] not in applied]

    if not pending:
        return

    logger.info("Found %d pending migration(s)", len(pending))

    for mig in pending:
        version = mig["version"]
        name = mig["name"]
        mod = mig["module"]

        logger.info("Applying migration %d: %s ...", version, name)

        try:
            phase = "DDL"
            # DDL phase (schema changes) - FK off for safety (SQLite only)
            if is_sqlite() and _owns_sqlite_transaction(mod):
                await _run_sqlite_self_managed(engine, mod)
            elif hasattr(mod, "upgrade"):
                async with engine.begin() as conn:
                    if is_sqlite():
                        await conn.execute(text("PRAGMA foreign_keys = OFF"))
                    await mod.upgrade(conn)
                    if is_sqlite():
                        await conn.execute(text("PRAGMA foreign_keys = ON"))

            phase = "seed"
            # Seed phase (data, uses ORM session)
            if hasattr(mod, "seed"):
                await mod.seed(session_factory)

            phase = "record"
            # Record as applied
            await _record_migration(engine, version, name)
        except Exception as exc:
            logger.error("Migration %d failed during %s (%s)", version, phase, type(exc).__name__)
            raise
        logger.info("Migration %d applied successfully", version)


async def run_all_migrations(engine, session_factory) -> bool:
    """Main entry point - called from init_db().

    Simple flow:
    1. Preflight an eligible local SQLite→PG source, then create_all()
    2. Ensure _migrations table
    3. Handle BamDude 3.0.1 upgrade (rename + bootstrap)
    4. Run all pending migrations
    5. Name an upstream Bambuddy database in the log, if one is sitting there

    A Bambuddy database found in the data directory is left exactly where it is:
    the import was removed in 0.5.6, and step 5 only reports it.
    """
    from backend.app.core.config import settings
    from backend.app.core.database import Base
    from backend.app.core.db_dialect import is_sqlite

    if is_sqlite():
        legacy_path = _find_legacy_database(settings.data_dir)

        # BamDude 3.0.1 upgrade: rename bambuddy.db → bamdude.db
        # Must check BEFORE create_all (engine may create empty bamdude.db on connect)
        if legacy_path and await _is_bamdude_301(legacy_path):
            db_path = Path(settings.data_dir) / "bamdude.db"
            logger.info("Found BamDude 3.0.1 database: %s - renaming to bamdude.db", legacy_path)
            await engine.dispose()
            if db_path.exists():
                db_path.unlink()
            legacy_path.rename(db_path)
            for suffix in ("-wal", "-shm"):
                wal = legacy_path.parent / (legacy_path.name + suffix)
                if wal.exists():
                    wal.rename(db_path.parent / (db_path.name + suffix))

    from backend.app.core.schema_repair import claim, retry_residuals
    from backend.app.core.schema_repair_state import SchemaRepairPending, fail_attempt

    # Existing replacement ownership wins before any local-source preparation.
    # In particular, never overwrite an AS-IS legacy import with current-model
    # create_all before its pending converters have resumed on PG. SQLite
    # keeps its pre-D4 create_all: some legacy migrations depend on its tables.
    debt = await claim(engine, Base.metadata)
    prepared = None
    if not is_sqlite() and debt is None:
        from backend.app.core.db_portable import preflight_sqlite_to_pg

        prepared = await preflight_sqlite_to_pg(engine)

    # An existing debt must spend its attempt before even create_all; an
    # exhausted installation must not perform another automatic DDL pass.
    try:
        created_schema = False
        if debt is None or is_sqlite():
            async with engine.begin() as conn:
                created_schema = not await conn.run_sync(lambda sync: inspect(sync).get_table_names())
                await conn.run_sync(Base.metadata.create_all)

        # Auto-migrate SQLite → PostgreSQL if PG is empty and local SQLite exists
        if not is_sqlite() and debt is None:
            from backend.app.core.db_portable import auto_migrate_sqlite_to_pg

            # A failed or declined import is not proof this is a new installation.
            if any(
                (Path(settings.data_dir) / name).exists() for name in ("bamdude.db", "bambuddy.db", "bambutrack.db")
            ):
                created_schema = False
            await auto_migrate_sqlite_to_pg(engine, Base.metadata, prepared=prepared or False)
    except Exception as exc:
        if debt is None:
            raise
        logger.error("Import schema bootstrap failed (%s)", type(exc).__name__)
        try:
            async with engine.begin() as conn:
                await fail_attempt(conn, debt, "bootstrap_failure")
        except Exception:
            logger.warning("Repair bootstrap failed; durable ownership remains pending")
        raise SchemaRepairPending("bootstrap_failure") from None
    finally:
        if prepared is not None:
            from backend.app.core.db_portable import _file_work

            await _file_work(prepared.close)

    # Spend a durable attempt BEFORE pending migrations as well as conform.
    # A killed child cannot refund it; an ordinary boot has no such debt.
    if debt is None:
        debt = await claim(engine, Base.metadata)
    try:
        await _ensure_migrations_table(engine)
        if await _has_existing_data(engine):
            await _bootstrap_existing(engine)
        await _run_pending(engine, session_factory)
    except Exception:
        if debt is None:
            raise
        try:
            async with engine.begin() as conn:
                await fail_attempt(conn, debt, "migration_failure")
        except Exception:
            logger.warning("Repair attempt failed; durable ownership remains pending")
        raise SchemaRepairPending("migration_failure") from None

    # A SQLite file imported this boot arrived at ITS migration level and the
    # chain has just brought it to the head; now add what the models declare
    # and no migration ever created. A no-op on every other boot.
    if debt is not None or not is_sqlite():
        from backend.app.core.db_portable import conform_imported_schema

        await conform_imported_schema(engine, Base.metadata, debt=debt)
    # Focused residual retry only. Do not reconcile a running native schema
    # against live models on each startup.
    await retry_residuals(engine, Base.metadata)

    # Every boot, not once, and on every engine: the FILE is what matters, and a
    # PostgreSQL install with a stray bambuddy.db in its data directory deserves
    # the same sentence (two stat calls). See _warn_if_foreign_bambuddy_file.
    await _warn_if_foreign_bambuddy_file(settings.data_dir)
    return created_schema


async def _has_existing_data(engine) -> bool:
    """Check if database has existing data (not a fresh create_all)."""
    try:
        async with engine.begin() as conn:
            # Check if printers table has any rows - if yes, this is an existing install
            result = await conn.execute(text("SELECT COUNT(*) FROM printers"))
            count = result.scalar() or 0
            return count > 0
    except Exception:
        return False


async def _is_bamdude_301(db_path: Path) -> bool:
    """Check if a legacy DB is BamDude 3.0.1 (has telegram_chats table)."""
    import aiosqlite

    try:
        async with aiosqlite.connect(str(db_path)) as db:
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='telegram_chats'")
            row = await cursor.fetchone()
            return row is not None
    except Exception:
        return False


def _find_legacy_database(data_dir) -> Path | None:
    """Find a legacy database (bambuddy.db or bambutrack.db)."""
    for name in ("bambuddy.db", "bambutrack.db"):
        path = Path(data_dir) / name
        if path.exists():
            return path
    return None
