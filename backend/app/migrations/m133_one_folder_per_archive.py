"""Make the folder ↔ archive link one-to-one.

``library_folders.archive_id`` was a plain nullable FK, so any number of folders
could point at the same archive. The archive side only ever showed the first one
it found (``linkedFolders[0]`` on the archives page), which meant a second
binding existed in the database and nowhere on screen.

⚠️ **Duplicates are resolved before the index, or the index cannot be created.**
Where several folders claim one archive, the **most recently updated** folder
keeps it and the others are cleared. Newest-wins because the later binding is
the more deliberate one — an older link is more likely to be the forgotten
half of the pair. Every clearing is logged with both folder names, since this
is the one moment an existing link disappears without anybody pressing
anything.

⚠️ **A unique INDEX, not a unique CONSTRAINT.** Adding a table constraint to
SQLite means recreating the table; an index is a one-statement change on both
back ends. Both also allow any number of NULLs in a unique index, which is
exactly what is needed: unlinked folders are the normal case.

The route refuses a duplicate with a 409 naming the folder that holds the
archive (``_assert_archive_unclaimed``). That is what people meet; this index
is what makes it true even if a future writer forgets to ask.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

version = 133
name = "one_folder_per_archive"

_INDEX = "ix_library_folders_archive_id_unique"


async def upgrade(conn):
    # Resolve existing duplicates first — newest binding wins.
    result = await conn.execute(
        text(
            "SELECT archive_id, COUNT(*) AS n FROM library_folders "
            "WHERE archive_id IS NOT NULL GROUP BY archive_id HAVING COUNT(*) > 1"
        )
    )
    duplicates = [row[0] for row in result.fetchall()]

    for archive_id in duplicates:
        rows = (
            await conn.execute(
                text("SELECT id, name FROM library_folders WHERE archive_id = :a ORDER BY updated_at DESC, id DESC"),
                {"a": archive_id},
            )
        ).fetchall()
        keeper, losers = rows[0], rows[1:]
        for loser in losers:
            logger.warning(
                "m133: archive %s was claimed by %d folders — keeping %r (id=%s), clearing %r (id=%s)",
                archive_id,
                len(rows),
                keeper[1],
                keeper[0],
                loser[1],
                loser[0],
            )
        await conn.execute(
            text("UPDATE library_folders SET archive_id = NULL WHERE archive_id = :a AND id != :keep"),
            {"a": archive_id, "keep": keeper[0]},
        )

    if duplicates:
        logger.info("m133: resolved %d archive(s) held by more than one folder", len(duplicates))

    await conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX} ON library_folders (archive_id)"))
