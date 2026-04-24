"""Spool metadata bump — purchase_date + filament_diameter + lot.

Three new columns on ``spool``:

* ``purchase_date DATETIME NULL`` — user-entered acquisition date.
  Distinct from ``created_at`` (the import timestamp); the inventory
  table prefers it for the default "Added" column.
* ``filament_diameter VARCHAR(8) NOT NULL DEFAULT '1.75'`` — one of
  ``'1.75'`` / ``'2.85'``. Existing rows backfill to ``'1.75'`` (the
  overwhelmingly common value for Bambu printers). UI exposes a dropdown
  in the edit dialog.
* ``lot INTEGER NULL`` — position inside a purchase bundle/batch
  ("partiya"). Null for solo additions. Quick-add's N-quantity path can
  auto-number lots 1..N via ``SpoolBulkCreate.auto_increment_lot``.

The UI also exposes a helper "price per spool" input that multiplies
against ``label_weight`` to derive ``cost_per_kg`` client-side — no new
column is needed for that path.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column

version = 20
name = "spool_purchase_date"


async def upgrade(conn):
    await add_column(conn, "spool", "purchase_date DATETIME")
    await add_column(conn, "spool", "filament_diameter VARCHAR(8) NOT NULL DEFAULT '1.75'")
    await add_column(conn, "spool", "lot INTEGER")


async def seed(session_factory):
    """Backfill filament_diameter on legacy rows.

    ``add_column`` sets the DEFAULT so fresh inserts land with ``'1.75'``,
    but pre-existing rows may carry NULL on SQLite (DEFAULT only applies
    to new inserts there). Belt-and-braces UPDATE covers both dialects.
    """
    async with session_factory() as db:
        await db.execute(
            text(
                "UPDATE spool SET filament_diameter = '1.75' WHERE filament_diameter IS NULL OR filament_diameter = ''"
            )
        )
        await db.commit()
