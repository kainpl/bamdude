"""Add swap-macro execution intent to the print queue.

Two fields paired together:
    * ``execute_swap_macros BOOLEAN NOT NULL DEFAULT 1`` — master toggle.
    * ``swap_macro_events TEXT NULL`` — JSON array of event keys
      (e.g. ``["swap_mode_start","swap_mode_change_table"]``) describing
      which swap events should fire for this item. Null = use defaults.

Only queue items whose printer has swap mode enabled should honour these
flags; the API route enforces that per-item. Dispatch-side execution logic
is not wired up yet.
"""

from backend.app.migrations.helpers import add_column

version = 8
name = "swap_macro_queue_fields"


async def upgrade(conn):
    await add_column(conn, "print_queue", "execute_swap_macros BOOLEAN DEFAULT 1")
    await add_column(conn, "print_queue", "swap_macro_events TEXT")
