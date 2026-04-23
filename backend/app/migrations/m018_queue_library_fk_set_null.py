"""Change ``print_queue.library_file_id`` FK from ON DELETE CASCADE to SET NULL.

The app-level `delete_file` endpoint already nulls the column before the
row goes away, so this migration is essentially belt-and-braces for
PostgreSQL installs where FKs are actually enforced. SQLite runs with
``PRAGMA foreign_keys`` off by default, so its FK rule is inert either
way — we still issue the equivalent recreate there so fresh SQLite dumps
line up with the model definition.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import recreate_table

version = 18
name = "queue_library_fk_set_null"


_NEW_DDL = """CREATE TABLE print_queue (
    id INTEGER PRIMARY KEY,
    queue_id INTEGER NOT NULL REFERENCES printer_queues(id),
    waiting_reason TEXT,
    archive_id INTEGER REFERENCES print_archives(id) ON DELETE CASCADE,
    library_file_id INTEGER REFERENCES library_files(id) ON DELETE SET NULL,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    position INTEGER NOT NULL DEFAULT 0,
    scheduled_time DATETIME,
    manual_start BOOLEAN NOT NULL DEFAULT 0,
    auto_off_after BOOLEAN NOT NULL DEFAULT 0,
    ams_mapping TEXT,
    plate_id INTEGER,
    bed_levelling BOOLEAN NOT NULL DEFAULT 1,
    flow_cali BOOLEAN NOT NULL DEFAULT 1,
    layer_inspect BOOLEAN NOT NULL DEFAULT 0,
    timelapse BOOLEAN NOT NULL DEFAULT 0,
    use_ams BOOLEAN NOT NULL DEFAULT 1,
    mesh_mode_fast_check BOOLEAN NOT NULL DEFAULT 1,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    execute_swap_macros BOOLEAN DEFAULT 1,
    swap_macro_events TEXT,
    batch_id VARCHAR(36),
    started_at DATETIME,
    completed_at DATETIME,
    error_message TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL
)"""

_COLUMNS_TO_COPY = (
    "id, queue_id, waiting_reason, archive_id, library_file_id, project_id, "
    "position, scheduled_time, manual_start, auto_off_after, ams_mapping, plate_id, "
    "bed_levelling, flow_cali, layer_inspect, timelapse, use_ams, mesh_mode_fast_check, "
    "status, execute_swap_macros, swap_macro_events, batch_id, "
    "started_at, completed_at, error_message, created_at, created_by_id"
)


async def upgrade(conn):
    if is_postgres():
        # Drop + re-add the FK with SET NULL. Constraint name follows the
        # default SQLAlchemy/PostgreSQL convention.
        await conn.execute(
            text(
                """
                DO $$
                DECLARE
                    conname TEXT;
                BEGIN
                    SELECT c.conname INTO conname
                    FROM pg_constraint c
                    JOIN pg_class t ON c.conrelid = t.oid
                    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
                    WHERE t.relname = 'print_queue' AND a.attname = 'library_file_id' AND c.contype = 'f'
                    LIMIT 1;
                    IF conname IS NOT NULL THEN
                        EXECUTE 'ALTER TABLE print_queue DROP CONSTRAINT ' || quote_ident(conname);
                    END IF;
                    ALTER TABLE print_queue
                        ADD CONSTRAINT print_queue_library_file_id_fkey
                        FOREIGN KEY (library_file_id)
                        REFERENCES library_files(id) ON DELETE SET NULL;
                END$$;
                """
            )
        )
    else:
        # SQLite — recreate table so its internal FK metadata matches the
        # model definition. PRAGMA foreign_keys is off in this install, so
        # the change is cosmetic, but keeps dumps/schema tooling honest.
        # Row-count assertion guards against silent data loss during the
        # copy-drop-rename dance — any mismatch aborts the migration with
        # a clear error rather than leaving a half-populated table behind.
        before = (await conn.execute(text("SELECT COUNT(*) FROM print_queue"))).scalar() or 0
        await recreate_table(conn, "print_queue", _NEW_DDL, _COLUMNS_TO_COPY)
        after = (await conn.execute(text("SELECT COUNT(*) FROM print_queue"))).scalar() or 0
        if after != before:
            raise RuntimeError(f"m018 recreate_table lost rows: before={before} after={after}")
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_print_queue_batch_id ON print_queue(batch_id)"))


async def seed(session_factory):  # pragma: no cover — no-op
    async with session_factory() as db:
        _ = db  # noqa: ARG001
