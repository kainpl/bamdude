"""Add ``virtual_printers.queue_force_color_match`` (upstream Bambuddy #1188).

Per-VP toggle that tells the auto-queue intake to extract per-slot
``type+color`` from each incoming 3MF and pin those values as
``force_color_match=True`` overrides on the resulting ``auto_queue_items``
row. Without the toggle, intake only persisted ``required_filament_types``
(a de-duplicated list of types) so the eligibility scheduler matched on
material but not colour — multi-printer farms with the same filament type
loaded in different colours could route a job onto the wrong machine.

Defaults to False so upgraders keep their existing routing behaviour;
operators flip it on per-VP from the VirtualPrinterCard. Idempotent —
``add_column`` is a no-op when the column already exists.
"""

from backend.app.migrations.helpers import add_column

version = 50
name = "vp_queue_force_color_match"


async def upgrade(conn):
    await add_column(
        conn,
        "virtual_printers",
        "queue_force_color_match BOOLEAN NOT NULL DEFAULT 0",
    )
