"""Add mesh_mode_fast_check flag to the print queue.

Captures operator intent per queue item: should the bed-mesh fast-check pass
run before this print? Default True (matches the slicer's default behaviour).

Downstream consumption — unpacking the 3MF on dispatch, patching the gcode,
and repacking — will be wired up in a follow-up migration/service. This
migration only persists the flag so the UI has a field to bind to today.
"""

version = 6
name = "mesh_mode_fast_check"


async def upgrade(conn):
    from backend.app.migrations.helpers import add_column

    await add_column(conn, "print_queue", "mesh_mode_fast_check BOOLEAN DEFAULT 1")
