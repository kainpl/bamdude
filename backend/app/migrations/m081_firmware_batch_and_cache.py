"""Create firmware cache-index + bulk-update history tables.

New tables only (no ALTER), so ``create_all()`` already makes them on fresh and
existing DBs alike (it runs before pending migrations in ``init_db``). Kept as an
explicit migration for the audit trail and so a DB provisioned with create_all
disabled still gets them. Idempotent via ``checkfirst`` — a no-op when the tables
already exist. Uses the ORM table definitions so it stays dialect-correct on
SQLite *and* PostgreSQL.
"""

from backend.app.models.firmware import FirmwareBatchItem, FirmwareBatchRun, FirmwareCacheEntry

version = 81
name = "firmware_batch_and_cache"


async def upgrade(conn):
    for model in (FirmwareCacheEntry, FirmwareBatchRun, FirmwareBatchItem):
        await conn.run_sync(model.__table__.create, checkfirst=True)
