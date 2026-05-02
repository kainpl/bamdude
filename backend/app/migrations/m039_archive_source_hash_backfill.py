"""Backfill ``print_archives.source_content_hash`` from ``content_hash``.

m009 introduced ``source_content_hash`` so BamDude could collapse
patched archives against their library originals via
``effective_hash = COALESCE(source_content_hash, content_hash)``. The
column was deliberately left NULL for two cases:

1. **Library-flow with no patches** — ``source_content_hash`` would
   equal ``content_hash``, so we kept it NULL "to avoid duplication."
2. **External prints** with no chain ancestor — same reasoning.

In practice this made the chain-of-custody invariant blurry: rows
with NULL source were "standalone today, possibly a chain root in
the future," and every dedup query had to ``COALESCE(...)`` to find
them. The 0.4.2 series tightens the invariant: **every row writes a
non-NULL ``source_content_hash``** — standalone rows seed it with
their own ``content_hash`` so they become the chain root for any
future patched variant.

This migration backfills that invariant for existing rows. After the
UPDATE, every archive row has ``source_content_hash`` set; the
``COALESCE`` guards in the query layer become a defence-in-depth
check for any hypothetical race between this migration and a
concurrent insert (which the migration runner serialises against
anyway).

Idempotent — the ``WHERE source_content_hash IS NULL`` clause makes
re-runs a no-op once the backfill has completed.

No DDL: column already exists from m009.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

version = 39
name = "archive_source_hash_backfill"


async def upgrade(conn):
    # No schema change. The column was added in m009.
    pass


async def seed(session_factory):
    async with session_factory() as db:
        result = await db.execute(
            text(
                "UPDATE print_archives "
                "SET source_content_hash = content_hash "
                "WHERE source_content_hash IS NULL "
                "AND content_hash IS NOT NULL"
            )
        )
        await db.commit()
        rowcount = getattr(result, "rowcount", None)
        if rowcount:
            logger.info("m039: backfilled source_content_hash on %s archive rows", rowcount)
