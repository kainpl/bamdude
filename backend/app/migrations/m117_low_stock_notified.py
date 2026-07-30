"""Remember that a spool's low-stock warning has already been sent.

``on_filament_low`` had no caller anywhere — the switch was in the notification
settings, enabled, and nothing ever fired it (dead upstream too, where the event
is defined twice and called zero times). It is wired up now, from the point where
a finished print's consumption is written back to the spool.

That trigger needs a memory. Without one the event repeats after *every* print
on a spool that is already below its threshold, which is the fastest way to teach
someone to switch notifications off. This column holds the "already warned" bit,
and the same helper clears it when the spool climbs back above the threshold —
so a refilled spool warns again next time it runs down, and only then.

Persistent rather than in-memory on purpose: a restart must not re-warn about
every low spool on the farm, the same reasoning that keeps per-print energy in
the database instead of a dict.

Note the singular table name — the model is ``Spool`` but ``__tablename__`` is
``spool``.
"""

from __future__ import annotations

from backend.app.migrations.helpers import add_column

version = 117
name = "low_stock_notified"


async def upgrade(conn):
    # Default 0 — every existing spool starts "not yet warned", so the first
    # print after the upgrade warns about the ones that are genuinely low. That
    # is a one-off catch-up, not spam: the flag then holds until a refill.
    await add_column(conn, "spool", "low_stock_notified BOOLEAN NOT NULL DEFAULT 0")
