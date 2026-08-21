"""Scanning an external folder becomes a job with a row, not a held-open request.

The scan held one write transaction from the first subfolder it discovered to
its final commit — the whole walk of the share in between. On a NAS that is
minutes, and SQLite lets nobody else write for the duration, so unrelated
queries failed with ``database is locked`` fifteen seconds in
(``PRAGMA busy_timeout``). Reported by a user whose share is a Synology mount.

⚠️ **No new permission.** Starting a scan is already gated by ``LIBRARY_UPLOAD``;
a second permission for watching one you were allowed to start would gate
nothing. That is why this migration has no ``seed``.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import table_exists

logger = logging.getLogger(__name__)

version = 148
name = "library_scan_jobs"


async def upgrade(conn):
    if await table_exists(conn, "library_scan_jobs"):
        return

    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME" if sqlite else "TIMESTAMP"

    await conn.execute(
        text(
            f"""
            CREATE TABLE library_scan_jobs (
                id {pk},
                folder_id INTEGER NOT NULL REFERENCES library_folders(id) ON DELETE CASCADE,
                status VARCHAR(16) NOT NULL DEFAULT 'queued',
                started_at {ts},
                finished_at {ts},
                files_total INTEGER NOT NULL DEFAULT 0,
                files_seen INTEGER NOT NULL DEFAULT 0,
                files_added INTEGER NOT NULL DEFAULT 0,
                files_updated INTEGER NOT NULL DEFAULT 0,
                files_removed INTEGER NOT NULL DEFAULT 0,
                folders_added INTEGER NOT NULL DEFAULT 0,
                folders_removed INTEGER NOT NULL DEFAULT 0,
                skipped_deletions BOOLEAN NOT NULL DEFAULT '0',
                error TEXT,
                created_by INTEGER,
                created_at {ts}
            )
            """
        )
    )
    await conn.execute(text("CREATE INDEX ix_library_scan_jobs_folder_id ON library_scan_jobs (folder_id)"))
    await conn.execute(text("CREATE INDEX ix_library_scan_jobs_status ON library_scan_jobs (status)"))
    # The question asked on every start: is one already running for this folder.
    await conn.execute(text("CREATE INDEX ix_library_scan_jobs_folder_status ON library_scan_jobs (folder_id, status)"))
    logger.info("m148: created library_scan_jobs")
