"""Auto-queue layer above per-printer print_queue.

Introduces a router/staging area for prints whose printer hasn't been
chosen yet. The full design lives in
``temp/auto-queue-adaptation-variants.md`` §12.

New table
---------
- ``auto_queue_items`` — pre-dispatch items with target_model + filament
  filters. Created by ``Base.metadata.create_all`` in ``init_db`` (the
  AutoQueueItem model is registered there); no explicit CREATE TABLE
  is needed in this migration. The model file is
  ``backend/app/models/auto_queue.py``.

New columns on existing tables
------------------------------
- ``print_queue.source_auto_item_id INTEGER NULL`` — back-reference to
  the originating auto-queue row when this item was created via
  AutoQueueScheduler.assign(). NULL for items added directly to a
  specific printer's queue (the existing flow). FK to
  ``auto_queue_items(id) ON DELETE SET NULL`` so cleaning up the auto
  table does not cascade-delete dispatched items.
- ``printer_queues.auto_distribute_eligible BOOLEAN NOT NULL DEFAULT TRUE``
  — opt-out flag for excluding a printer from auto-distribution
  (maintenance, reserved-for-manual, etc.). Existing rows backfill to
  TRUE so behaviour is unchanged on upgrade.

Indexes (declared here, not on the model — they are perf hints, not
data invariants):
- ``ix_auto_queue_status_position`` on ``(status, position)`` —
  scheduler tick reads pending rows ordered by position.
- ``ix_auto_queue_batch_id`` on ``(batch_id)`` — batch-cancel /
  batch-reorder lookups. The model already declares ``index=True`` on
  this column, so SQLAlchemy will create an equivalent index via
  ``create_all``; the ``IF NOT EXISTS`` guard here is belt-and-braces
  for installs where ``create_all`` ran before the column was indexed.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column, table_exists

version = 24
name = "auto_queue"


async def upgrade(conn):
    # New columns on existing tables (no-op if create_all + an upgraded
    # codebase already added them on a fresh install).
    await add_column(
        conn,
        "print_queue",
        "source_auto_item_id INTEGER REFERENCES auto_queue_items(id) ON DELETE SET NULL",
    )
    await add_column(
        conn,
        "printer_queues",
        "auto_distribute_eligible BOOLEAN NOT NULL DEFAULT TRUE",
    )

    # Indexes on the new auto_queue_items table. Guard with table_exists
    # in case this runs on a very old install where create_all hasn't seen
    # the new model yet (init_db imports it, so this should always be True
    # on a normal startup, but the guard is cheap).
    if await table_exists(conn, "auto_queue_items"):
        for ddl in [
            "CREATE INDEX IF NOT EXISTS ix_auto_queue_status_position ON auto_queue_items(status, position)",
            "CREATE INDEX IF NOT EXISTS ix_auto_queue_batch_id ON auto_queue_items(batch_id)",
        ]:
            await conn.execute(text(ddl))


async def seed(session_factory):  # pragma: no cover — no-op
    """No seed: auto_queue_items starts empty, printer_queues backfill is
    handled by the column DEFAULT."""
    async with session_factory() as db:
        _ = db  # noqa: ARG001
