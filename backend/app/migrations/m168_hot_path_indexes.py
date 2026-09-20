"""Indexes for the four hot reads that were sorting or scanning whole tables.

Every one below was chosen from a measured plan, not a guess — `EXPLAIN QUERY
PLAN` on a copy of a real farm's database, before and after. An index that did
not change a plan is not here, because an index nobody's query reaches is pure
cost on every insert.

What the plans said (SQLite; PostgreSQL's planner makes the same choices):

* **The archive list** — `deleted_at IS NULL ORDER BY created_at DESC LIMIT n`
  used the `deleted_at` index and then `USE TEMP B-TREE FOR ORDER BY`: it sorted
  the entire filtered set on every page. `(deleted_at, created_at)` turns that
  into a covering index walk that stops at the LIMIT. This is the farm-scale
  "the archive is slow" report — at 888 rows it is invisible, at tens of
  thousands it is the whole complaint.
* **Hide duplicates** — `GROUP BY COALESCE(source_content_hash, content_hash)`
  built a `TEMP B-TREE FOR GROUP BY` across the table on every page load. An
  index on that exact expression removes the temp structure. Both engines index
  expressions; the doubled parentheses are what PostgreSQL requires and SQLite
  accepts (verified on both).
* **Archives of one printer** — `printer_id` was not used at all: the planner
  entered through `deleted_at` and sorted afterwards.
* **The inventory page** — `selectinload(Spool.k_profiles)` fetched every page's
  profiles with `SCAN spool_k_profile`, a full table scan on each open, because
  the link table carried no index whatsoever.

`CREATE INDEX IF NOT EXISTS` (the m140/m159 idiom): idempotent on both engines,
nothing to refuse over, no seed. Fresh installs get the same indexes from the
models' ``__table_args__`` via ``create_all``.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

version = 168
name = "hot_path_indexes"


_INDEXES = (
    (
        "ix_print_archives_active_created",
        "CREATE INDEX IF NOT EXISTS ix_print_archives_active_created ON print_archives (deleted_at, created_at)",
    ),
    (
        "ix_print_archives_printer_created",
        "CREATE INDEX IF NOT EXISTS ix_print_archives_printer_created ON print_archives (printer_id, created_at)",
    ),
    (
        "ix_print_archives_effective_hash",
        "CREATE INDEX IF NOT EXISTS ix_print_archives_effective_hash "
        "ON print_archives ((COALESCE(source_content_hash, content_hash)))",
    ),
    (
        "ix_spool_k_profile_spool_id",
        "CREATE INDEX IF NOT EXISTS ix_spool_k_profile_spool_id ON spool_k_profile (spool_id)",
    ),
)


async def upgrade(conn):
    for index_name, statement in _INDEXES:
        await conn.execute(text(statement))
        logger.info("m168: ensured %s", index_name)
