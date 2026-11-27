"""A printer's place in the order AutoQueue jobs pick printers (C01, upstream #2915).

``printers.dispatch_priority`` is read only when the setting ``model_dispatch_order`` is
``priority``: the lowest number is picked first among printers equally fit for a job, and a
printer without one goes last. Nullable with no default and no seed: no printer has a place
until the operator sets one, so every farm keeps routing as before this migration.
``services/printer_pick_order.py`` is its only reader for routing.
"""

from backend.app.migrations.helpers import add_column

version = 271
name = "printer_dispatch_priority"


async def upgrade(conn):
    await add_column(conn, "printers", "dispatch_priority INTEGER")
