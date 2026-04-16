"""Bambuddy 2.2.2 → BamDude 3.0.1 data import.

This migration runs ONLY when a legacy bambuddy.db/bambutrack.db is found.
If no legacy DB exists, this migration is a no-op.

Uses seed() instead of upgrade() because import needs its own engine.begin()
transactions (import_bambuddy_data manages its own connection lifecycle).
"""

import logging
from pathlib import Path

version = 0
name = "bambuddy_to_bamdude_301"

logger = logging.getLogger(__name__)


async def seed(session_factory):
    """Import data from legacy Bambuddy database if found."""
    from backend.app.core.config import settings
    from backend.app.core.database import engine

    # Find legacy database
    legacy_path = None
    for db_name in ("bambuddy.db", "bambutrack.db"):
        path = Path(settings.data_dir) / db_name
        if path.exists():
            legacy_path = path
            break

    if not legacy_path:
        logger.info("No legacy Bambuddy database found - skipping import")
        return

    # Check it's actually Bambuddy (not BamDude 3.0.1 which was already renamed)
    import aiosqlite

    try:
        async with aiosqlite.connect(str(legacy_path)) as db:
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='telegram_chats'")
            row = await cursor.fetchone()
            if row is not None:
                logger.info("Legacy DB has telegram_chats - BamDude 3.0.1 already handled. Skipping.")
                return
    except Exception:
        return

    logger.info("Found Bambuddy 2.2.2 database: %s - importing data", legacy_path)

    from backend.app.migrations.import_bambuddy import import_bambuddy_data

    await import_bambuddy_data(engine, legacy_path)
    logger.info("Bambuddy import complete")
