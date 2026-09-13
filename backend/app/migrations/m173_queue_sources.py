"""m173: the queue source spool — one table, two per-job columns, one FK rule.

Spec: ``60-specs/queue-source-spool-spec.md`` §4 (files and data model), §8
(legacy and rollout). Task 1 of the implementation plan: the foundation only.
Nothing here captures a file — the writer, the spool root and the background
hydration arrive in later tasks.

**DDL and local DML only — no SMB I/O, no copying, no backfill** (§8). A
migration that walked a farm's library shares on startup would block the boot it
is part of, and the bytes an old queue row was enqueued with are not necessarily
the bytes on that path today (§8: "if the revision is absent, do not promise").
Existing rows therefore come out of this migration with both new columns NULL —
which is exactly the ``legacy`` state the API reports (§8) and the marker the
bounded background hydration looks for.

DDL is dialect-neutral, on m162's pattern, even though ``queue_sources`` is in
``Base.metadata`` and ``create_all`` therefore builds it on every fresh install
before the chain is reached. The guard below then finds it and does nothing; the
CREATE is the path an EXISTING database walks, and on PostgreSQL that path must
parse — see ``tests/unit/test_migration_ddl_dialects.py`` for the install that
rule was written after.

⚠️ **``archive_id`` stops cascading.** Both queues declared
``ON DELETE CASCADE`` on it, so on PostgreSQL purging an archive deleted every
queue row that named it: the one cascade that destroyed a *job* because its
*source* went away — precisely what this feature exists to survive (§4, §10).
The models now say SET NULL and the PostgreSQL constraint is rewritten below
(m018's ``DO $$`` shape, which does the same thing for ``library_file_id``).

⚠️ **On SQLite the rule is not rewritten, and cannot usefully be.** This
codebase never sets ``PRAGMA foreign_keys = ON``, so no SQLite install has ever
enforced either rule — the stored text is documentation. Changing it would mean
a ``recreate_table`` of ``print_queue``: a copy-drop-rename of the live queue
with ~50 columns re-listed by hand, risking real work to correct a comment.
What an upgraded SQLite file needs instead is the **code-level detach** beside
each delete path (the convention ``services/part_stock.py`` sets), and that
lands in **Task 9** of this plan. A SQLite file that later moves to PostgreSQL
gets the rule for free: ``db_portable._reconcile_foreign_keys`` drops and
re-adds any key whose ON DELETE differs from the model's.

⚠️ **No ``seed()``.** There is nothing to seed and nothing to convert; see the
"no backfill" paragraph above.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres, is_sqlite
from backend.app.migrations.helpers import add_column, json_column_type, table_exists

version = 173
name = "queue_sources"

#: Both tiers get the same two columns: the per-printer queue and the router.
_QUEUE_TABLES = ("print_queue", "auto_queue_items")


async def upgrade(conn):
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME DEFAULT CURRENT_TIMESTAMP" if sqlite else "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    stamp = "DATETIME" if sqlite else "TIMESTAMP"

    if not await table_exists(conn, "queue_sources"):
        # The three CHECKs and the UNIQUE are part of the table, not an
        # afterthought: SQLite cannot add either to an existing table without
        # rebuilding it, so a constraint written for fresh installs only would
        # never reach an upgraded database. The names match the models so that
        # ``db_portable._reconcile_check_constraints`` — which matches by name —
        # can still add them to a file imported from somewhere that lacked them.
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE queue_sources (
                id {pk},
                sha256 VARCHAR(64) NOT NULL,
                size_bytes INTEGER NOT NULL,
                relative_path VARCHAR(512) NOT NULL,
                format VARCHAR(8) NOT NULL,
                state VARCHAR(16) NOT NULL DEFAULT 'ready',
                created_at {ts} NOT NULL,
                unreferenced_at {stamp},
                CONSTRAINT uq_queue_sources_sha256 UNIQUE (sha256),
                CONSTRAINT ck_queue_sources_size_bytes_non_negative CHECK (size_bytes >= 0),
                CONSTRAINT ck_queue_sources_format CHECK (format IN ('3mf', 'gcode')),
                CONSTRAINT ck_queue_sources_state CHECK (state IN ('ready', 'deleting', 'broken'))
            )
            """
        )

    # Outside the table guard and IF NOT EXISTS on both dialects, for the same
    # reason m162 gives: a fresh install already has it from ``create_all``, and
    # a database that somehow has the table without the index still ends up with
    # it. One index, one read: the GC's candidate scan (§9).
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_queue_sources_state_unreferenced_at ON queue_sources (state, unreferenced_at)"
    )

    snapshot_type = json_column_type()
    for table in _QUEUE_TABLES:
        # RESTRICT: a blob a job still names may not be deleted under it (S5).
        await add_column(conn, table, "queue_source_id INTEGER REFERENCES queue_sources(id) ON DELETE RESTRICT")
        await add_column(conn, table, f"source_snapshot {snapshot_type}")
        # Every "who still owns this blob" question is a lookup by this column
        # across both tables, and the GC asks it before every unlink (§9).
        await conn.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_queue_source_id ON {table} (queue_source_id)"
        )

    if is_postgres():
        # See the module docstring: SQLite never enforced either rule, so only
        # the dialect that does gets rewritten. m018 is the pattern — the
        # constraint name is looked up rather than assumed, because a database
        # that has already been through a rebuild may carry a generated one.
        for table in _QUEUE_TABLES:
            await conn.execute(
                text(
                    f"""
                    DO $$
                    DECLARE
                        existing TEXT;
                    BEGIN
                        SELECT c.conname INTO existing
                        FROM pg_constraint c
                        JOIN pg_class t ON c.conrelid = t.oid
                        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
                        WHERE t.relname = '{table}' AND a.attname = 'archive_id' AND c.contype = 'f'
                        LIMIT 1;
                        IF existing IS NOT NULL THEN
                            EXECUTE 'ALTER TABLE {table} DROP CONSTRAINT ' || quote_ident(existing);
                        END IF;
                        ALTER TABLE {table}
                            ADD CONSTRAINT {table}_archive_id_fkey
                            FOREIGN KEY (archive_id)
                            REFERENCES print_archives(id) ON DELETE SET NULL;
                    END$$;
                    """
                )
            )
