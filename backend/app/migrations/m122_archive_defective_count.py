"""Record how many parts of a print came out unusable.

``print_archives.quantity`` says how many objects were on the plate — it is
filled from the 3MF (``archive.quantity = len(printable_objects)``), not typed
in. What was missing is the other half of that number: how many of them were
scrap. Operators tracked it in the notes field or not at all, which means it
could not be counted, filtered or compared across prints.

The column is operator-owned and starts at zero on every existing row. There is
nothing to backfill: the information was never recorded anywhere a migration
could read it, and guessing from ``failure_reason`` would be wrong in both
directions — a failed print is not N defective parts, and a completed print can
still yield scrap.

It is raised automatically when objects are skipped mid-print, from the
printer's own ``s_obj`` report rather than from our skip command, so a skip made
on the printer's screen counts too. That path only ever raises the value, never
lowers it, so an operator's own figure survives.

**Not wired into any total.** ``quantity`` keeps meaning "objects on the plate",
and the project/statistics sums that read it are untouched. Netting scrap out of
"printed" would silently restate numbers people have already been looking at,
and the decision was to keep the two figures side by side instead.
"""

from __future__ import annotations

import logging

from backend.app.migrations.helpers import add_column

logger = logging.getLogger(__name__)

version = 122
name = "archive_defective_count"


async def upgrade(conn):
    if await add_column(conn, "print_archives", "defective_count INTEGER NOT NULL DEFAULT 0"):
        logger.info("m122: added print_archives.defective_count")
