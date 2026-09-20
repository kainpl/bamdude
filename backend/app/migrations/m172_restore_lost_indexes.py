"""Restore four indexes that only migrations ever created — and later rebuilds lost.

``ix_auth_eph_type_exp`` (m012), ``ix_auth_rl_type_time`` (m012),
``ix_auto_queue_status_position`` (m024) and ``ix_slicer_pipelines_is_deleted``
(m102) are declared in no model, so ``create_all`` never makes them and
``conform_imported_schema`` cannot restore them. On SQLite they vanished the
first time a later migration ran ``recreate_table`` on their table: the helper
copied the rows into a fresh table built from the DDL alone and dropped the old
one, indexes included (fixed in the helper alongside this migration — it now
carries the old table's indexes over). A long-lived install therefore lacks
them while a fresh one has them; a schema diff on 2026-09-09 found the gap.

Idempotent by construction (``IF NOT EXISTS``), valid on SQLite and PostgreSQL,
skipped for a table that does not exist yet (the chain also runs on a fresh
install right after ``create_all``, where every table is present). Performance
only — the token sweep, the rate-limit window and the auto-queue pick — never
correctness.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import table_exists

version = 172
name = "restore_lost_indexes"

INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_auth_eph_type_exp", "auth_ephemeral_tokens", "token_type, expires_at"),
    ("ix_auth_rl_type_time", "auth_rate_limit_events", "event_type, occurred_at"),
    ("ix_auto_queue_status_position", "auto_queue_items", "status, position"),
    ("ix_slicer_pipelines_is_deleted", "slicer_pipelines", "is_deleted"),
)


async def upgrade(conn):
    for index_name, table, columns in INDEXES:
        if not await table_exists(conn, table):
            continue
        await conn.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({columns})"))
