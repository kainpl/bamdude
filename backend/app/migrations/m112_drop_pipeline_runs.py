"""Drop the pipeline run/fanout half; keep the saved preset bundle.

Upstream Bambuddy #1425 ("Slicer Pipelines", ported in 0.4.7b2) bundled two
things: a named, reusable set of the four SliceModal preset picks, and an engine
that sliced a source with them and fanned N copies out over a printer or a whole
model class.

The second half duplicates machinery BamDude already had. ``AutoQueueItem``
carries ``quantity`` (N items under a shared ``batch_id``), ``target_model``
routing, filament/colour matching and batch cancel, and the AutoQueue
distributor assigns printers — none of which upstream has, because upstream has
no auto-queue at all. That absence is precisely why their pipelines had to do
the fanout themselves; on our two-tier queue it was a second way to do the same
job, which is worse than either way alone.

What is kept is the half AutoQueue structurally cannot provide: AutoQueue's
input is an already-sliced archive or library file, so it can never carry the
recipe used to slice. ``slicer_pipelines`` stays as that recipe.

Removed here:
  - tables ``pipeline_jobs`` and ``pipeline_runs`` (jobs first — FK to runs);
  - the target/fanout columns on ``slicer_pipelines``, which mean nothing
    without a run engine;
  - the ``pipelines:run`` permission from stored group lists.

Safe to drop rather than migrate: the feature only ever shipped in the 0.4.7
betas b2-b5, never in a stable release, so no stable-channel database holds run
history. Saved bundles themselves are untouched.
"""

from backend.app.migrations.helpers import column_exists, get_table_columns, recreate_table, table_exists

version = 112
name = "drop_pipeline_runs"

_DEAD_COLUMNS = ("target_kind", "target_printer_id", "target_model_class", "fanout_strategy")

_PIPELINES_DDL = """
CREATE TABLE slicer_pipelines (
    id INTEGER NOT NULL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    description VARCHAR(1000),
    printer_preset_source VARCHAR(20) NOT NULL,
    printer_preset_id VARCHAR(200) NOT NULL,
    process_preset_source VARCHAR(20) NOT NULL,
    process_preset_id VARCHAR(200) NOT NULL,
    filament_presets_json TEXT NOT NULL,
    bed_type VARCHAR(64),
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    is_deleted BOOLEAN DEFAULT 0 NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
)
"""

_KEPT_COLUMNS = (
    "id, name, description, printer_preset_source, printer_preset_id, "
    "process_preset_source, process_preset_id, filament_presets_json, bed_type, "
    "created_by, is_deleted, created_at, updated_at"
)


async def upgrade(conn):
    # Jobs before runs — pipeline_jobs carries the FK to pipeline_runs.
    for table in ("pipeline_jobs", "pipeline_runs"):
        if await table_exists(conn, table):
            await conn.exec_driver_sql(f"DROP TABLE {table}")

    if not await table_exists(conn, "slicer_pipelines"):
        return

    # Only rebuild when at least one dead column is actually present, so a fresh
    # install (created by create_all from the trimmed model) is left alone.
    present = [c for c in _DEAD_COLUMNS if await column_exists(conn, "slicer_pipelines", c)]
    if not present:
        return

    # recreate_table copies by name, so guard against an older DB that predates a
    # column this DDL declares — copy only what both sides actually have.
    existing = set(await get_table_columns(conn, "slicer_pipelines"))
    columns = ", ".join(c for c in (c.strip() for c in _KEPT_COLUMNS.split(",")) if c in existing)
    await recreate_table(conn, "slicer_pipelines", _PIPELINES_DDL, columns)


async def seed(session_factory):
    """Strip ``pipelines:run`` from stored group permission lists.

    Column-explicit read + Core update (see feedback_migration_seed_columns): an
    entity-wide ``select(Group)`` would emit columns a later migration may not
    have added yet and break the upgrade chain.

    Unlike a grant, this runs for **every** group including custom ones: the
    permission no longer exists in ``Permission``, so leaving it behind would
    keep a dangling string in the stored list forever.
    """
    from sqlalchemy import select, update

    from backend.app.models.group import Group

    async with session_factory() as db:
        result = await db.execute(select(Group.id, Group.permissions))
        dirty = 0
        for row in result.all():
            perms = list(row.permissions or [])
            if "pipelines:run" not in perms:
                continue
            await db.execute(
                update(Group).where(Group.id == row.id).values(permissions=[p for p in perms if p != "pipelines:run"])
            )
            dirty += 1
        if dirty:
            await db.commit()
