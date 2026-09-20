"""Who put this queue row here — so "queue finished" stops lying.

Since 0.5.4 every print holds a ``print_queue`` row while it runs: the
scheduler's own, the claim a direct print takes at dispatch
(``queue_batch.claim_printer_for_direct_print``), or one created at print start
for a print BamDude never sent (``main.mark_queue_printing_for_printer``). That
was the point — it serialises tier 1 and gives Repeat something to re-arm.

The cost was a meaning nobody re-checked. ``on_print_complete`` fires
``queue_completed`` / ``printer_queue_completed`` from inside its
``if queue_item:`` branch, on the reasoning — written when it was true — that
"only a finished *queue* item can empty the queue (an external print never
consumes a queue item)". Every print consumes one now, so both notifications
started arriving after prints nobody scheduled. Confirmed on a farm's own log:
an externally-started print sent "queue finished — all jobs done" in the same
slot a genuinely queued one does.

``origin`` says which of the three it is. Deliberately on the row rather than
derived at completion: ``plate_hold.answer_by_repeating`` re-arms THE SAME ROW
instead of copying it, so a repeat inherits the origin by construction — a
repeated external print stays external and still raises no queue event, and a
repeated queue item still counts. Deriving it would have to re-decide that, and
would get it wrong the moment a repeat looks like a fresh pending row (which is
exactly what a repeat is).

Existing rows become ``'queue'``. They are almost all history, and for the few
still printing at upgrade time the old behaviour is the honest default: we do
not know what they were, and pretending otherwise would silence a real queue.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column

version = 160
name = "print_queue_origin"


async def upgrade(conn):
    # VARCHAR over an enum on purpose — the two backends disagree about enum
    # DDL, and every other small vocabulary in this table (status,
    # *_cali_mode, preheat_override) is a plain string for the same reason.
    await add_column(conn, "print_queue", "origin VARCHAR(16) DEFAULT 'queue'")
    # ``add_column`` is a no-op when the column already exists, and a DEFAULT
    # only fills rows inserted after it — so backfill explicitly rather than
    # trusting the default to have reached the existing table.
    await conn.execute(text("UPDATE print_queue SET origin = 'queue' WHERE origin IS NULL"))
