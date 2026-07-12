"""Add ``archived`` / ``archived_at`` columns to ``printers``.

Soft-retire a printer without deleting it: an archived printer disappears from
the whole app (Printers page, every picker, queues, dispatch, scheduler, MQTT,
metrics, background jobs) while its ``print_archives`` are fully preserved. It
is restorable from Settings → Printers.

``archived`` is an independent axis from ``is_active`` (Maintenance Mode, #1476):
Maintenance parks a printer temporarily and keeps it visible; archival retires it
and hides it entirely. The "available" predicate everywhere becomes
``is_active AND NOT archived``.

Default False / NULL so existing printers are unaffected. Fresh installs get the
columns from the model's ``create_all``; this backfills existing DBs.

See docs spec ``2026-07-12-archived-printers-design.md``.
"""

from backend.app.migrations.helpers import add_column

version = 105
name = "printer_archived"


async def upgrade(conn):
    await add_column(conn, "printers", "archived BOOLEAN DEFAULT 0 NOT NULL")
    await add_column(conn, "printers", "archived_at TIMESTAMP")
