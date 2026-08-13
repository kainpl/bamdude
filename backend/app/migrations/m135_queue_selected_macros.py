"""Per-item macro selection for the print dialog.

``selected_macro_ids TEXT NULL`` — JSON array of ``macros.id``, the exact
analogue of ``swap_macro_events``. It records which non-swap macros the
operator ticked for this job.

Added to both queue tables: ``auto_queue_items`` is what the distributor
copies the per-printer row from, so a column on only one of them would drop
the selection on the way through.

NULL on every existing row, and that is the whole meaning of the feature:
macros are opt-in per print, so an item queued before this migration — like
one dispatched by any path that has no dialog — runs none of them. No seed,
because there is no honest value to backfill: nobody chose anything.
"""

from backend.app.migrations.helpers import add_column

version = 135
name = "queue_selected_macros"


async def upgrade(conn):
    await add_column(conn, "print_queue", "selected_macro_ids TEXT")
    await add_column(conn, "auto_queue_items", "selected_macro_ids TEXT")
