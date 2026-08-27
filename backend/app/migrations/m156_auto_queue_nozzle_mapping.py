"""Slicer's nozzle pick survives the auto-queue tier.

``print_queue.nozzle_mapping`` (#1780) preserves the slicer's per-filament
physical-nozzle array for dual-nozzle-rack models, but a print sent to a
VP in ``auto_queue`` mode had nowhere to keep it: the router row lacked the
column, so the distributor promoted the item without it and the firmware
auto-picked nozzles the operator did not choose. Same Text/JSON shape as the
per-printer column; the AutoQueueScheduler copies it verbatim on assignment.
"""

from backend.app.migrations.helpers import add_column

version = 156
name = "auto_queue_nozzle_mapping"


async def upgrade(conn):
    await add_column(conn, "auto_queue_items", "nozzle_mapping TEXT")
