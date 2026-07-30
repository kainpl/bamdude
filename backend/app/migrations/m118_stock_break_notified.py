"""Remember when a SKU's stock-break alert last went out.

``on_stock_break_alert`` was scaffolded by m059 and left without a trigger on
purpose — that migration says so in as many words: *"no backend trigger fires
them today; ForecastPanel.tsx renders alerts client-side ... so a future
scheduled aggregator can flip them live without a schema change"*. This is that
aggregator, and the one thing it does need is a memory.

The alert answers "this filament runs out before your replenishment arrives",
which is a slow-moving fact. A checker that runs on a timer would otherwise
repeat it every pass, and the operator would switch the channel off before the
filament ever ran out. This column holds the last time each SKU was announced;
the service re-states a standing break once a day and clears the stamp when the
SKU recovers, so coming back into the state announces itself immediately.

It lives on ``filament_sku_settings`` rather than in a table of its own because
that table is already the per-SKU row, keyed on the same
(material, subtype, brand, color_name) tuple, and already persists for SKUs with
no spools. The service upserts a row when it has to alert about a SKU that has
no settings yet; every column it creates takes the default the UI already
assumes for a missing row (lead time 0, 14-day margin), so an operator sees no
change from a row appearing.
"""

from __future__ import annotations

from backend.app.migrations.helpers import add_column

version = 118
name = "stock_break_notified"


async def upgrade(conn):
    # NULL = never announced. Deliberately not backfilled to "now": on the first
    # pass after upgrade every SKU genuinely in break should say so once.
    await add_column(conn, "filament_sku_settings", "stock_break_notified_at TIMESTAMP")
