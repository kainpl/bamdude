"""The "this colour ran out" alert's own memory, beside the break and reorder stamps.

A SKU whose every spool is archived stays in the forecast for 90 days (the
panel's retention rule) with zero stock, so it read as a stock break: "will run
out before replenishment arrives, 0 g left — order immediately", repeated every
day. A farm that moved a colour to another brand got that message daily for
every brand it stopped buying, while kilograms of the same filament sat on the
shelf (owner, 2026-11-20).

Such a SKU is now silent while the same material, subtype and colour is on the
shelf under another brand, and otherwise is told once that it ran out. "Once"
needs a stamp of its own: the break stamp repeats daily, and sharing it would
either repeat the ran-out text or silence a later break of the same SKU.
"""

from __future__ import annotations

from backend.app.migrations.helpers import add_column

version = 250
name = "stock_depleted_notified"


async def upgrade(conn):
    # NULL = not announced in the current episode.
    await add_column(conn, "filament_sku_settings", "stock_depleted_notified_at TIMESTAMP")
