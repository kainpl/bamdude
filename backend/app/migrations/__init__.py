"""BamDude migration system — auto-discovered versioned migrations.

Three startup modes:
1. Fresh install: create_all + run seeds
2. Import from Bambuddy: create_all + import data + run seeds
3. BamDude upgrade: create_all (new tables) + run pending migrations
"""

import importlib
import logging
import pkgutil
from pathlib import Path

from sqlalchemy import text

logger = logging.getLogger(__name__)


def _discover_migrations() -> list[dict]:
    """Find all m*.py migration files, sorted by version."""
    migrations = []
    package = importlib.import_module("backend.app.migrations")
    for _importer, modname, _ispkg in pkgutil.iter_modules(package.__path__):
        if modname.startswith("m") and len(modname) > 3 and modname[1:4].isdigit():
            mod = importlib.import_module(f"backend.app.migrations.{modname}")
            if hasattr(mod, "version") and hasattr(mod, "name"):
                migrations.append({
                    "version": mod.version,
                    "name": mod.name,
                    "module": mod,
                })
    migrations.sort(key=lambda m: m["version"])
    return migrations


async def _ensure_migrations_table(engine) -> None:
    """Create _migrations table if it doesn't exist."""
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS _migrations (
                id INTEGER PRIMARY KEY,
                version INTEGER NOT NULL UNIQUE,
                name VARCHAR(100) NOT NULL,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """))


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
    """For existing BamDude installs that predate the migration system.

    If _migrations table is empty, mark version 1 as applied
    (schema is already current from previous run_migrations).
    """
    applied = await _get_applied_versions(engine)
    if not applied:
        migrations = _discover_migrations()
        if migrations and migrations[0]["version"] == 1:
            await _record_migration(engine, 1, migrations[0]["name"])
            logger.info("Bootstrapped existing install at migration version 1")


async def _run_pending(engine, session_factory) -> None:
    """Discover and run all pending migrations."""
    applied = await _get_applied_versions(engine)
    migrations = _discover_migrations()
    pending = [m for m in migrations if m["version"] not in applied]

    if not pending:
        return

    logger.info("Found %d pending migration(s)", len(pending))

    for mig in pending:
        version = mig["version"]
        name = mig["name"]
        mod = mig["module"]

        logger.info("Applying migration %d: %s ...", version, name)

        # DDL phase (schema changes) — FK off for the entire upgrade
        if hasattr(mod, "upgrade"):
            async with engine.begin() as conn:
                await conn.execute(text("PRAGMA foreign_keys = OFF"))
                await mod.upgrade(conn)
                await conn.execute(text("PRAGMA foreign_keys = ON"))

        # Seed phase (data, uses ORM session)
        if hasattr(mod, "seed"):
            await mod.seed(session_factory)

        # Record as applied
        await _record_migration(engine, version, name)
        logger.info("Migration %d applied successfully", version)


async def run_all_migrations(engine, session_factory) -> None:
    """Main entry point — called from init_db().

    Startup modes:
    1. Fresh install (no bamdude.db, no legacy DB) → create_all + m001
    2. Import from Bambuddy 2.2.2 (bambuddy.db without telegram_chats) → create fresh + import
    3. Upgrade from BamDude 3.0.1 (bambuddy.db WITH telegram_chats) → rename + m002
    4. BamDude 3.1.1+ upgrade (bamdude.db exists) → pending migrations
    """
    from backend.app.core.config import settings
    from backend.app.core.database import Base

    db_path = Path(settings.data_dir) / "bamdude.db"
    legacy_path = _find_legacy_database(settings.data_dir)

    if db_path.exists():
        # Mode 4: existing BamDude install
        logger.info("Found existing BamDude database")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        await _ensure_migrations_table(engine)
        await _bootstrap_existing(engine)
        await _run_pending(engine, session_factory)

    elif legacy_path:
        # Legacy DB found — determine if BamDude 3.0.1 or Bambuddy 2.2.2
        is_bamdude_301 = await _is_bamdude_301(legacy_path)

        if is_bamdude_301:
            # Mode 3: BamDude 3.0.1 → rename and upgrade
            logger.info("Found BamDude 3.0.1 database: %s — renaming to bamdude.db", legacy_path)
            legacy_path.rename(db_path)
            # Also rename WAL/SHM files if present
            for suffix in ("-wal", "-shm"):
                wal = legacy_path.parent / (legacy_path.name + suffix)
                if wal.exists():
                    wal.rename(db_path.parent / (db_path.name + suffix))

            # Recreate engine connection to renamed file
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            await _ensure_migrations_table(engine)
            # Bootstrap at version 1 (3.0.1 schema = baseline)
            await _bootstrap_existing(engine)
            # m002 will apply 3.0.1 → 3.1.1 changes
            await _run_pending(engine, session_factory)
        else:
            # Mode 2: Bambuddy 2.2.2 → import into fresh DB
            logger.info("Found Bambuddy 2.2.2 database: %s — importing to BamDude", legacy_path)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            from backend.app.migrations.import_bambuddy import import_bambuddy_data

            await import_bambuddy_data(engine, legacy_path)
            logger.info("Bambuddy import complete")

            await _ensure_migrations_table(engine)
            # Schema is already current (create_all), only run seeds (m001)
            # Mark all upgrade-only migrations as applied (they target older schemas)
            all_migrations = _discover_migrations()
            for mig in all_migrations:
                if mig["version"] > 1:
                    await _record_migration(engine, mig["version"], mig["name"])
                    logger.info("Skipped migration %d (schema already current from import)", mig["version"])
            await _run_pending(engine, session_factory)  # runs m001 only
    else:
        # Mode 1: fresh install
        logger.info("No database found — creating fresh BamDude database")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        await _ensure_migrations_table(engine)
        # Schema is current (create_all), mark upgrade-only migrations as applied
        all_migrations = _discover_migrations()
        for mig in all_migrations:
            if mig["version"] > 1:
                await _record_migration(engine, mig["version"], mig["name"])
        await _run_pending(engine, session_factory)  # runs m001 (seeds) only


async def _is_bamdude_301(db_path: Path) -> bool:
    """Check if a legacy DB is BamDude 3.0.1 (has telegram_chats table)."""
    import aiosqlite

    try:
        async with aiosqlite.connect(str(db_path)) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='telegram_chats'"
            )
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
