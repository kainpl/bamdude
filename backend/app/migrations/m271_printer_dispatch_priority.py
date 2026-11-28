"""A printer's place in the order AutoQueue jobs pick printers (C01, upstream #2915).

``printers.dispatch_priority`` is read only when the setting ``model_dispatch_order`` is
``priority``: the lowest number is picked first among printers equally fit for a job, and a
printer without one goes last. Nullable with no default and no seed: no printer has a place
until the operator sets one, so every farm keeps routing as before this migration.
``services/printer_pick_order.py`` is its only reader for routing.

The same order's «idle the longest» reads each printer's newest finished print on every
AutoQueue pass, every rebalancing pass and on assign-now. ``ix_print_archives_printer_completed``
on ``(printer_id, completed_at)`` lets that read stop at the newest row that is a print:
measured at 100 000 archives on SQLite (acceptance r1 A1-01), the grouped maximum over
``ix_print_archives_printer_created`` walked every row (~0.7 s), the per-printer newest row over
this index answers in under a millisecond with the same answers. ``CREATE INDEX IF NOT EXISTS``
(the m168 idiom); fresh installs get it from the model's ``__table_args__``.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column

version = 271
name = "printer_dispatch_priority"


async def upgrade(conn):
    await add_column(conn, "printers", "dispatch_priority INTEGER")
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_print_archives_printer_completed "
            "ON print_archives (printer_id, completed_at)"
        )
    )
