"""The reorder alert's own memory, alongside the stock-break one.

``on_stock_reorder_alert`` is the fourth of the notification events that shipped
fully wired — provider column, per-chat Telegram toggle, en+uk templates — and
were never called by anything. It hid longer than the others because the comment
above the method demonstrates the call as ``notification_service
.on_stock_reorder_alert(...)``, so a search for call sites finds the
documentation and reports the event as live.

It is the earlier, softer sibling of the stock break: *stock has reached the
reorder point*, i.e. what is left no longer covers the lead time plus the safety
buffer. The two are mutually exclusive by construction — once a SKU is actually
going to run out before replenishment, that is the message worth sending — so
each needs its own stamp, or a SKU sliding from one state to the other would go
quiet for a day.

A separate column rather than an extension of m118 because m118 may already have
been applied on a development database, and migrations are only ever added.
"""

from __future__ import annotations

from backend.app.migrations.helpers import add_column

version = 119
name = "stock_reorder_notified"


async def upgrade(conn):
    # NULL = never announced, same reasoning as m118.
    await add_column(conn, "filament_sku_settings", "stock_reorder_notified_at TIMESTAMP")
