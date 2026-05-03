"""Drop the ``pending_uploads`` table.

After m041 drained every legacy pending row to the library and the
post-0.4.2 cleanup wave removed the route module + model + frontend
panel + Layout badge, the table holds no live state — it's just
historical audit rows (``status='archived'`` / ``'discarded'``) with no
remaining consumer. We drop it outright.

Idempotent — ``DROP TABLE IF EXISTS`` is safe on:

* fresh installations (no model means ``Base.metadata.create_all``
  never materialised the table; m041 short-circuits via its
  ``table_exists`` guard; this migration sees nothing to drop);
* upgraded installations (table populated by past traffic, drained by
  m041 in the same boot, dropped here);
* re-runs (table already gone from a prior boot — no-op).
"""

from sqlalchemy import text

version = 42
name = "drop_pending_uploads"


async def upgrade(conn):
    # `IF EXISTS` makes this a no-op on installations where the table
    # was never materialised (e.g. fresh install on a future release
    # that has already removed the model + database.py import).
    await conn.execute(text("DROP TABLE IF EXISTS pending_uploads"))
