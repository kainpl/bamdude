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

        # DDL phase (schema changes)
        if hasattr(mod, "upgrade"):
            async with engine.begin() as conn:
                await mod.upgrade(conn)

        # Seed phase (data, uses ORM session)
        if hasattr(mod, "seed"):
            await mod.seed(session_factory)

        # Record as applied
        await _record_migration(engine, version, name)
        logger.info("Migration %d applied successfully", version)


async def run_all_migrations(engine, session_factory) -> None:
    """Main entry point — called from init_db().

    Handles all three startup modes:
    1. Fresh install (no DB) → create_all + migrations
    2. Import from Bambuddy (legacy DB found) → create_all + import + migrations
    3. BamDude upgrade (existing DB) → create_all + pending migrations
    """
    from backend.app.core.config import settings
    from backend.app.core.database import Base

    db_path = Path(settings.data_dir) / "bamdude.db"

    # Check for legacy Bambuddy database
    legacy_path = _find_legacy_database(settings.data_dir)
    is_fresh = not db_path.exists()

    # Mode 2: Import from Bambuddy
    if is_fresh and legacy_path:
        logger.info("Found legacy database: %s — importing to BamDude", legacy_path)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Import data from old DB
        from backend.app.migrations.import_bambuddy import import_bambuddy_data

        await import_bambuddy_data(engine, legacy_path)
        logger.info("Legacy import complete")
    else:
        # Mode 1 (fresh) or Mode 3 (upgrade)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    # Ensure _migrations table and run pending
    await _ensure_migrations_table(engine)

    if not is_fresh and not legacy_path:
        # Mode 3: existing BamDude install — bootstrap if needed
        await _bootstrap_existing(engine)

    await _run_pending(engine, session_factory)


def _find_legacy_database(data_dir) -> Path | None:
    """Find a legacy Bambuddy database for import."""
    for name in ("bambuddy.db", "bambutrack.db"):
        path = Path(data_dir) / name
        if path.exists():
            return path
    return None
