"""Return ``require_previous_success`` to the per-printer queue tier, and add
the acknowledgement that makes it escapable.

**Why the column comes back.** m002 stripped ``require_previous_success`` from
``print_queue`` when the queue was split into two tiers, and it survived only on
``AutoQueueItem``. Nothing has ever read it since — the API accepts it, stores
it and hands it back, and no scheduler consults it. "Don't run this if the last
print failed" is a safety option, and a safety option that silently does nothing
is worse than one that is absent: the user believes the farm is guarded. Both
tiers dispatch prints, so both tiers have to honour the gate.

**Why ``gate_acknowledged`` ships with it.** The gate looks back at the last
finished print on the printer, so a single failure blocks *every* gated item
behind it until some print succeeds there. Upstream met the same wall and added
this flag (their #1818): a failure marked acknowledged drops out of the lookback,
so an operator who has fixed the physical problem does not have to re-queue the
rest of the day's work. We reach it through the existing ``unskip`` action —
putting a gate-skipped item back in the queue *is* the statement "I have dealt
with that failure", so no second control has to be invented for it.

Numbered 116, skipping 115: ``m115_zigbee_plug`` already holds that version on
``feature/mqtt-plug-control``, and two different m115s would collide when the
branches meet. A gap in the sequence is harmless — migrations are discovered and
ordered by their own ``version``.
"""

from __future__ import annotations

from backend.app.migrations.helpers import add_column

version = 116
name = "require_previous_success"


async def upgrade(conn):
    # Default 0: an existing queue must not suddenly start gating items the user
    # queued under the old behaviour.
    await add_column(conn, "print_queue", "require_previous_success BOOLEAN NOT NULL DEFAULT 0")
    # Default 0 on history too — every past failure stays visible to the
    # lookback. Acknowledging is a deliberate act, never a backfill.
    await add_column(conn, "print_queue", "gate_acknowledged BOOLEAN NOT NULL DEFAULT 0")
