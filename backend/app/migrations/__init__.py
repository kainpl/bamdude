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

from sqlalchemy import text

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


async def _run_pending(engine, session_factory) -> None:
    """Discover and run all pending migrations sequentially."""
    from backend.app.core.config import settings as app_settings

    applied = await _get_applied_versions(engine)
    migrations = _discover_migrations()

    # Dev mode: re-run the latest migration every time (for iterating on schema/seeds)
    if app_settings.debug and migrations and applied:
        latest = max(m["version"] for m in migrations)
        if latest in applied:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM _migrations WHERE version = :v"), {"v": latest})
            applied.discard(latest)
            logger.info("Dev mode: re-running migration %d", latest)

    pending = [m for m in migrations if m["version"] not in applied]

    if not pending:
        return

    logger.info("Found %d pending migration(s)", len(pending))

    for mig in pending:
        version = mig["version"]
        name = mig["name"]
        mod = mig["module"]

        logger.info("Applying migration %d: %s ...", version, name)

        # DDL phase (schema changes) - FK off for safety (SQLite only)
        if hasattr(mod, "upgrade"):
            async with engine.begin() as conn:
                from backend.app.core.db_dialect import is_sqlite

                if is_sqlite():
                    await conn.execute(text("PRAGMA foreign_keys = OFF"))
                await mod.upgrade(conn)
                if is_sqlite():
                    await conn.execute(text("PRAGMA foreign_keys = ON"))

        # Seed phase (data, uses ORM session)
        if hasattr(mod, "seed"):
            await mod.seed(session_factory)

        # Record as applied
        await _record_migration(engine, version, name)
        logger.info("Migration %d applied successfully", version)


async def run_all_migrations(engine, session_factory) -> None:
    """Main entry point - called from init_db().

    Simple flow:
    1. create_all() - ensures all tables exist from models
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

    # Create/update all tables from models
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Auto-migrate SQLite → PostgreSQL if PG is empty and local SQLite exists
    if not is_sqlite():
        from backend.app.core.db_portable import auto_migrate_sqlite_to_pg

        await auto_migrate_sqlite_to_pg(engine, Base.metadata)

    # Ensure _migrations table
    await _ensure_migrations_table(engine)

    # Check if this is an existing install (has user tables with data)
    is_existing = await _has_existing_data(engine)
    if is_existing:
        await _bootstrap_existing(engine)

    # Run all pending migrations sequentially
    await _run_pending(engine, session_factory)

    # Every boot, not once: see _warn_if_foreign_bambuddy_file.
    if is_sqlite():
        await _warn_if_foreign_bambuddy_file(settings.data_dir)


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
