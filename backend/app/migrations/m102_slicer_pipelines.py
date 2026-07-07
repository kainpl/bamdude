"""Slicer Pipelines (upstream Bambuddy #1425) — three new tables + permission seed.

Adds ``slicer_pipelines`` (saved 4-slot preset bundle + dispatch target), ``pipeline_runs``
(one "run pipeline" invocation), and ``pipeline_jobs`` (one row per copy). Column names
mirror upstream for schema/frontend parity; ``pipeline_jobs.auto_queue_item_id`` is the
BamDude adaptation that lets a model-class copy defer to our AutoQueue distributor (see
[[feedback_queue_ideology]]) instead of upstream's flat-queue ``target_model`` item.

Fresh installs get the tables/indexes from ``create_all`` (the models are registered in
``database.py``); the guarded DDL below is a no-op there and only runs on existing DBs.
``seed()`` grants the three new ``pipelines:*`` permissions to the system groups so
existing installs pick them up (Administrators + Operators get all three; Viewers get read).
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import table_exists

version = 102
name = "slicer_pipelines"


async def upgrade(conn):
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME" if sqlite else "TIMESTAMP"
    false_ = "0" if sqlite else "FALSE"

    if not await table_exists(conn, "slicer_pipelines"):
        await conn.execute(
            text(
                f"""
                CREATE TABLE slicer_pipelines (
                    id {pk},
                    name VARCHAR(200) NOT NULL,
                    description VARCHAR(1000),
                    printer_preset_source VARCHAR(20) NOT NULL,
                    printer_preset_id VARCHAR(200) NOT NULL,
                    process_preset_source VARCHAR(20) NOT NULL,
                    process_preset_id VARCHAR(200) NOT NULL,
                    filament_presets_json TEXT NOT NULL,
                    bed_type VARCHAR(64),
                    target_kind VARCHAR(20) NOT NULL DEFAULT 'printer_class',
                    target_printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL,
                    target_model_class VARCHAR(20),
                    fanout_strategy VARCHAR(20) NOT NULL DEFAULT 'max_parallel',
                    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    is_deleted BOOLEAN NOT NULL DEFAULT {false_},
                    created_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_slicer_pipelines_is_deleted ON slicer_pipelines (is_deleted)")
    )

    if not await table_exists(conn, "pipeline_runs"):
        await conn.execute(
            text(
                f"""
                CREATE TABLE pipeline_runs (
                    id {pk},
                    pipeline_id INTEGER REFERENCES slicer_pipelines(id) ON DELETE SET NULL,
                    source_library_file_id INTEGER REFERENCES library_files(id) ON DELETE SET NULL,
                    source_archive_id INTEGER REFERENCES print_archives(id) ON DELETE SET NULL,
                    parent_run_id INTEGER REFERENCES pipeline_runs(id) ON DELETE SET NULL,
                    copies INTEGER NOT NULL DEFAULT 1,
                    status VARCHAR(20) NOT NULL DEFAULT 'queued',
                    slice_job_id INTEGER,
                    sliced_library_file_id INTEGER REFERENCES library_files(id) ON DELETE SET NULL,
                    eligibility_overridden BOOLEAN NOT NULL DEFAULT {false_},
                    error_message TEXT,
                    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at {ts},
                    completed_at {ts}
                )
                """
            )
        )
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_pipeline_runs_pipeline_id ON pipeline_runs (pipeline_id)"))

    if not await table_exists(conn, "pipeline_jobs"):
        await conn.execute(
            text(
                f"""
                CREATE TABLE pipeline_jobs (
                    id {pk},
                    pipeline_run_id INTEGER NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
                    copy_index INTEGER NOT NULL DEFAULT 0,
                    assigned_printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL,
                    queue_entry_id INTEGER REFERENCES print_queue(id) ON DELETE SET NULL,
                    auto_queue_item_id INTEGER REFERENCES auto_queue_items(id) ON DELETE SET NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    error_message TEXT,
                    dispatched_at {ts},
                    completed_at {ts}
                )
                """
            )
        )
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_pipeline_jobs_run_id ON pipeline_jobs (pipeline_run_id)"))


async def seed(session_factory):
    """Grant the three pipelines:* permissions to existing system groups.

    Column-explicit read + Core update (see feedback_migration_seed_columns): an
    entity-wide ``select(Group)`` would emit not-yet-existing columns and break an
    upgrade chain where this migration runs before a later column lands.
    """
    from sqlalchemy import select, update

    from backend.app.models.group import Group

    all_perms = ["pipelines:read", "pipelines:write", "pipelines:run"]

    async with session_factory() as db:
        result = await db.execute(select(Group.id, Group.name, Group.is_system, Group.permissions))
        dirty = 0
        for row in result.all():
            perms = list(row.permissions or [])
            existing = set(perms)

            if row.is_system and row.name in ("Administrators", "Operators"):
                wanted = all_perms
            elif row.is_system and row.name == "Viewers":
                wanted = ["pipelines:read"]
            else:
                wanted = []

            to_add = [p for p in wanted if p not in existing]
            if to_add:
                await db.execute(update(Group).where(Group.id == row.id).values(permissions=perms + to_add))
                dirty += 1
        if dirty:
            await db.commit()
