"""Add ``print_queue.dispatch_attempts`` — start-watchdog wedge-retry counter.

Counts how many times the start-watchdog reverted a row from ``printing`` back
to ``pending`` because the printer accepted the file but never actually started.
Capped at ``DISPATCH_MAX_ATTEMPTS`` (``services/print_scheduler``) so a genuinely
wedged printer is failed instead of retried forever, and stops eating an upload
slot the rest of the farm is queueing for (#2555).

Fresh installs get the column from the model's ``create_all``; this adds it to
existing DBs. ``NOT NULL DEFAULT 0`` so every existing row starts the count at 0
(a NULL would make ``dispatch_attempts + 1`` NULL and silently disable the cap —
the scheduler also guards with ``(x or 0) + 1`` as belt-and-braces). Idempotent
(``add_column`` no-ops when the column exists, incl. the DEBUG-startup re-run).
"""

from backend.app.migrations.helpers import add_column

version = 108
name = "print_queue_dispatch_attempts"


async def upgrade(conn):
    await add_column(conn, "print_queue", "dispatch_attempts INTEGER NOT NULL DEFAULT 0")
