"""Empty the two queue tables once, to shed rows that outlived their prints.

**This deletes every row in ``print_queue`` and ``auto_queue_items``.** The
release notes tell operators to let their prints and queues finish before
upgrading, because this runs unattended on first start.

**What went wrong.** A queue item is moved out of ``printing`` by the print's own
completion handler. When that handler did not run — the connection dropped
across the end of a print, a swap macro was wrongly declared failed, a
housekeeping sweep closed the archive first — the row stayed ``printing``
forever. Nothing ever revisits it: the auto-clean that removes finished items
fires from the completion path, which is precisely the path that did not
happen. So the leftovers accumulate, one per interrupted print.

Operators saw the result as ``Queue 3 (printer 3) has 2 'printing' rows`` in the
log, hundreds of times an hour, and as a queue card showing work that had long
since printed. One farm's log carries two such rows on two separate printers
simultaneously, hours old.

The defects that create them are fixed. This is only about the rows those
defects already left behind, in databases we cannot inspect.

**Why empty rather than repair.** A stale ``printing`` row and a live one are the
same row. What separates them is the printer's physical state at this instant,
which a migration does not have and must not guess at — "keep the newest per
queue" would be right most of the time, and the rest of the time it silently
deletes the job someone is watching print. There is no heuristic here worth the
one case it gets wrong.

The alternative that is actually safe is to make the ambiguity impossible, and
that is what the release note does: finish everything first, and then no row is
live by definition. Emptying is then exact rather than clever.

**Nothing historical is lost.** Queue items are working state, not records. Print
history lives in ``print_archives``, and the queue's own completed / failed /
cancelled / total counters have rolled off that table via ``archive.queue_id``
since m019 — the model says so where the counters are declared. Only the two
live-state counters are cached on the queue row, and they are reset below.

**``printer_queues`` is corrected, not emptied.** The rows themselves must stay:
one exists per printer and the scheduler expects it. But the fields describing
what the queue is *doing* would otherwise survive the items they described, and
one of them is load-bearing — ``status='printing'`` is the dispatch claim that
``print_scheduler`` reads into ``busy_printers``. Left behind, it claims the
printer forever and the farm quietly stops taking work, which is a worse bug
than the one being fixed here.

``is_paused`` and ``auto_distribute_eligible`` are deliberately untouched: those
are the operator's configuration, not run state, and resetting them would
silently re-enable a printer someone had set aside.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

version = 120
name = "clear_queues"


async def upgrade(conn):
    # Counted before deleting so the log says what was actually removed. An
    # upgrade that empties tables should be able to prove what it took.
    queued = (await conn.execute(text("SELECT COUNT(*) FROM print_queue"))).scalar() or 0
    auto = (await conn.execute(text("SELECT COUNT(*) FROM auto_queue_items"))).scalar() or 0

    # auto_queue_items first: its rows point at print_queue rows once routed, so
    # clearing the referencing side first keeps the FK happy on every dialect
    # rather than relying on cascade behaviour that differs between them.
    await conn.execute(text("DELETE FROM auto_queue_items"))
    await conn.execute(text("DELETE FROM print_queue"))

    # Live state only. `status` is the dispatch claim — a queue left saying
    # 'printing' with no items is a printer that never takes work again.
    await conn.execute(
        text("UPDATE printer_queues SET status = 'idle', current_item_id = NULL, pending_count = 0, skipped_count = 0")
    )

    logger.info(
        "m120: cleared %d queue item(s) and %d auto-queue item(s); reset every printer queue to idle",
        queued,
        auto,
    )
