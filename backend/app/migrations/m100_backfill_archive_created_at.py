"""Backfill NULL print_archives.created_at (upstream Bambuddy #1732 / b1719806).

Legacy bambuddy.db-rename installs and rows copied via the SQLite<->Postgres
cross-DB import (db_portable.import_sqlite_to_postgres, raw INSERT bypassing
server_default=func.now()) can carry created_at=NULL. The archives list response
models require a datetime -> a single NULL row 500s GET /archives. One-time
backfill to the best available timestamp. Paired with schemas/archive.py making
created_at Optional so a future NULL-leaking path can't break the list again.

Idempotent (WHERE created_at IS NULL); only the "now" literal is dialect-branched.
No model/column change — print_archives already has created_at + completed_at +
started_at (models/archive.py). Runs inside engine.begin() (migration runner) so
the plain UPDATE is transactional — no begin_nested needed (BamDude uses numbered
migrations instead of upstream's inline run_migrations step).
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_sqlite

version = 100
name = "backfill_archive_created_at"


async def upgrade(conn):
    now_expr = "datetime('now')" if is_sqlite() else "NOW()"
    await conn.execute(
        text(
            f"UPDATE print_archives SET created_at = COALESCE(completed_at, started_at, {now_expr}) "
            "WHERE created_at IS NULL"
        )
    )
