"""Clear out the byte-identical duplicates that accumulated before dedup existed.

From this release a file that is already in the library is never stored twice.
What was collected before that is a one-time mess, and every install has it
exactly once — which is why this is a migration rather than a button. A button
would be permanent UI for a task nobody ever performs twice, and it would only
work for operators who knew to look for it.

⚠️ **Soft-delete only. Nothing is merged, nothing is re-pointed.** A merge would
have to reconcile four uniqueness constraints — ``library_file_makerworld_meta``
is 1:1 per file, tags and projects are unique pairs, and a plan item is unique
per (project, file) *and carries a copy count and an order*, so merging means
summing or choosing. And a hash duplicate is not always a duplicate to the person
who filed it: two MakerWorld profiles can produce byte-identical 3MFs. Setting
``deleted_at`` leaves every foreign key intact — ``print_archives`` keeps its
link, queue rows keep theirs — and it is **reversible**, which is the only reason
doing this unasked to somebody else's library is acceptable.

⚠️ **It cannot fail on data we have never seen**, which is the bar a migration
has to clear here: ``_run_pending`` has no ``try/except`` and records the version
only after success, so anything that can raise is a permanent startup outage on
someone else's farm. ``deleted_at = now`` violates nothing.

The survivor is the row something points at — a print history, a queued job, a
project, a note. When every copy is referenced, the lowest ``id`` wins. That
ordering is what makes the 30-day trash retention safe to leave alone: whatever
the sweeper eventually takes was the copy nothing pointed at anyway.

The rule and the bulk implementation live in ``services/library_ingest.py``, so
the migration and the endpoint cannot drift into two answers — which is the same
mistake this whole feature exists to undo.
"""

import logging

logger = logging.getLogger(__name__)

version = 141
name = "dedupe_existing_library_files"


async def upgrade(conn):
    """No DDL — this migration only moves rows to the trash."""
    return


async def seed(session_factory):
    from backend.app.services.library_ingest import trash_duplicate_rows

    async with session_factory() as session:
        groups, trashed = await trash_duplicate_rows(session)
        await session.commit()

    if trashed:
        logger.info(
            "m141: %d duplicate file(s) across %d group(s) moved to the library trash — "
            "the copy something pointed at was kept, and anything here can be restored",
            trashed,
            groups,
        )
    else:
        logger.info("m141: no byte-identical duplicates in the library")
