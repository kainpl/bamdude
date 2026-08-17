"""An index for the question every ingest now asks.

Deduplication looks a content hash up on every arrival — upload, API, ZIP
extraction, slicer output, the Virtual Printer, and now every file on every
mounted folder on every scan. That lookup deserves an index.

⚠️ **Deliberately NOT unique.** A unique index would express the invariant
exactly, and would refuse to build on any install that already holds
byte-identical rows. ``migrations/__init__.py::_run_pending`` has no
``try/except`` around ``upgrade()``/``seed()`` and records the version only
*after* success — so that refusal is not "the index was skipped", it is an
install that never starts again, retrying and failing on every boot, with
migrations frozen so this one can never be repaired in place. A pre-existing
duplicate is harmless; a farm that will not start is not.

The guarantee that a ninth ingest path cannot silently create duplicates lives
in the source guard (``test_every_library_file_construction_syncs_its_tags``),
where it fails the person who wrote the path rather than the person running the
program. What that trade gives up is a check-then-insert race between two
concurrent uploads producing one extra row — precisely the harmless thing that
already exists on other installs.

Partial on ``deleted_at IS NULL`` because that is exactly the lookup: a trashed
sibling was deleted by the user and must never pin a fresh arrival to itself.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

version = 140
name = "library_file_hash_index"


async def upgrade(conn):
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_library_files_file_hash_active "
            "ON library_files (file_hash) WHERE deleted_at IS NULL"
        )
    )
    logger.info("m140: indexed library_files.file_hash for active rows")
