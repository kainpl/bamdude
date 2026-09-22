"""Short, archive-scoped write guards shared by completion and archive facts.

Archive facts are edited from the archive page, the completion card and the
Telegram draft.  PostgreSQL needs an explicit transaction-scoped guard; SQLite
needs to obtain its sole writer before its authoritative read.  Call this
before loading the archive whose facts will be changed and keep filesystem,
MQTT and Telegram I/O outside the scope.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict
from contextlib import asynccontextmanager

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive

_archive_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_archive_file_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@asynccontextmanager
async def archive_write_scope(db: AsyncSession, archive_id: int):
    """Serialize one archive's short fact mutation until the caller commits.

    The local lock closes the in-process SQLite window.  A file-backed SQLite
    deployment additionally obtains ``BEGIN IMMEDIATE`` before any
    authoritative read, so a second process cannot read a stale snapshot and
    later overwrite it.  PostgreSQL uses a transaction-scoped advisory lock;
    the row lock in :func:`load_active_archive_for_write` is the durable
    backstop and makes the order visible to normal SQL writers too.

    SQLite callers must enter before their first authoritative read.  If an
    explicit caller-owned transaction already exists, this helper preserves it;
    public archive-fact writers are responsible for taking that transaction
    before their own read.
    """
    async with _archive_locks[archive_id]:
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            # Namespace 182 is reserved for completion/archive fact writers.
            await db.execute(text("SELECT pg_advisory_xact_lock(182, :archive_id)"), {"archive_id": archive_id})
        elif dialect == "sqlite" and not db.in_transaction():
            await db.execute(text("BEGIN IMMEDIATE"))
        elif dialect == "sqlite":
            # The active transaction may be an explicit caller-owned write
            # transaction (safe), or a deferred read (unsafe).  SQLAlchemy
            # cannot distinguish those portably, so the public writers all
            # enter before reading and tests pin that discipline.
            pass
        yield


@asynccontextmanager
async def archive_file_reference_scope(db: AsyncSession, file_path: str):
    """Serialize attach reuse with deletion's final shared-file refcount.

    Callers already own their archive scope, so the lock order is always
    archive-id then file-path.  SQLite's enclosing writer transaction is the
    cross-process guard; PostgreSQL needs a stable advisory key for rows that
    share bytes but not an archive id.
    """

    async with _archive_file_locks[file_path]:
        if db.get_bind().dialect.name == "postgresql":
            lock_key = int.from_bytes(hashlib.sha256(file_path.encode()).digest()[:8], "big", signed=True)
            await db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})
        yield


async def load_active_archive_for_write(db: AsyncSession, archive_id: int) -> PrintArchive | None:
    """Read the current non-trashed archive under :func:`archive_write_scope`."""
    return await db.scalar(
        select(PrintArchive)
        .where(PrintArchive.id == archive_id, PrintArchive.deleted_at.is_(None))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
