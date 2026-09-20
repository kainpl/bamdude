"""Where a queued print came from, when the auto-queue moved it to another printer model.

``auto_queue_items.rebalanced_at`` / ``rebalanced_from_model`` are stamped by
``services/queue_rebalance.py`` on every row it converts or creates: the panel
shows «← P1S» off them, and the scheduler reads ``MAX(rebalanced_at)`` per order
line as its cooldown. Nullable, no backfill — a row nobody moved says nothing,
which is the truth for every row written before this migration. Neither column
takes part in routing, dispatch or the stock ledger.
"""

from backend.app.migrations.helpers import add_column

version = 171
name = "auto_queue_rebalance"


async def upgrade(conn):
    await add_column(conn, "auto_queue_items", "rebalanced_at TIMESTAMP")
    await add_column(conn, "auto_queue_items", "rebalanced_from_model VARCHAR(64)")
