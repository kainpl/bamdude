"""Add ``printer_queues.is_paused`` — operator-controlled queue pause.

``PrinterQueue.status`` (idle / printing / paused / error) tracks what the
printer is doing and is the authoritative busy marker the print scheduler
and auto-queue read for double-dispatch protection. It can't double as a
manual pause: pausing a queue mid-print would have to overwrite
``status='printing'`` and make the printer look free.

``is_paused`` is a separate, operator-owned flag with two states —
running (dispatch + new items accepted) and paused (dispatch halted, new
items refused; a print already running keeps going). It's orthogonal to
``status`` so a queue can be ``printing`` and ``is_paused`` at once.

Idempotent: guarded by ``add_column``. Safe under ``DEBUG=true``
latest-migration re-runs.
"""

from backend.app.migrations.helpers import add_column, table_exists

version = 67
name = "printer_queue_is_paused"


async def upgrade(conn) -> None:
    if not await table_exists(conn, "printer_queues"):
        return
    await add_column(conn, "printer_queues", "is_paused BOOLEAN NOT NULL DEFAULT 0")
