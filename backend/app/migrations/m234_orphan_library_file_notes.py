"""Notes left behind by purged library files are removed (G11 follow-up, owner 2026-11-16).

``library_file_notes.library_file_id`` is declared ``ON DELETE CASCADE``, which
PostgreSQL honours and SQLite does not (this codebase never sets ``PRAGMA
foreign_keys = ON``). Two purge paths deleted library files in bulk, by
statement, without their notes: the trash retention sweep, and the deletion of
a user together with their items. On SQLite every file purged that way left its
notes in the table, naming a file that no longer exists. Both paths now remove
the notes in code; this removes what they left before that.

The seed deletes every note whose ``library_file_id`` matches no row of
``library_files``. A file in the trash still has its row, so its notes stay —
they come back with it. On PostgreSQL the cascade means there is nothing to
find; the statement is the same and removes nothing. A second run finds nothing.

⚠️ Not decided here, because stored data cannot decide it: SQLite hands a new
row ``max(id) + 1``, so a file created after the newest one was purged may have
taken its id — and with it the purged file's notes, which now read as its own.
Nothing in either table says which notes were written for which incarnation of
an id, so such notes are left where they are.

Tables and columns are named here: an ORM model would bring today's columns
into a step of the chain where they may not exist.
"""

import logging

logger = logging.getLogger(__name__)

version = 234
name = "orphan_library_file_notes"


async def upgrade(conn):
    """No schema change; the orphan notes are removed by seed."""


async def seed(session_factory):
    from sqlalchemy import Integer, column, delete, select, table

    files = table("library_files", column("id", Integer))
    notes = table("library_file_notes", column("id", Integer), column("library_file_id", Integer))

    async with session_factory() as db:
        result = await db.execute(delete(notes).where(notes.c.library_file_id.not_in(select(files.c.id))))
        await db.commit()
        removed = result.rowcount or 0
    if removed:
        logger.info("m234: removed %d note(s) of library files that no longer exist", removed)
