"""Record the real on-disk mtime for external library files and folders (#2680).

Sorting the File Manager by date put external (mapped / NAS) files in a
near-random order instead of ``ls -t``'s newest-first. Nothing captured the
files' on-disk mtime: the sort keyed off the DB ``updated_at`` / ``created_at``,
which for a bulk external scan is **the same scan instant for every row**. So a
whole block tied and ordered arbitrarily, and only rows BamDude had later touched
individually looked "partially right" — which is what made it read as a display
quirk rather than as missing data.

Nullable, with no backfill. There is nothing to backfill *from*: the value lives
on disk, not in the database, and the external scan writes it on the next pass.
Readers must ``COALESCE(fs_modified_at, updated_at)`` so three cases all keep a
sort key — internal uploads (no external file at all), external rows scanned
before this column existed, and folders that are not external.

The same column goes on ``library_folders`` because the folder tree's "sort by
recent activity" has to aggregate the same signal, and a directory has an mtime
of its own that is meaningful when its own children have not changed.
"""

from backend.app.migrations.helpers import add_column

version = 129
name = "library_fs_modified_at"


async def upgrade(conn):
    # ``TIMESTAMP``, not ``DATETIME``: PostgreSQL has no DATETIME type and
    # ``_to_postgres_column_def`` only rewrites boolean defaults and the rowid
    # alias, so the spelling reaches the server untranslated. Both dialects
    # accept TIMESTAMP (SQLite gives it the same NUMERIC affinity DATETIME gets),
    # which is why m105 and m118 use it.
    #
    # No default — "unknown" is exactly what a missing value means here, and a
    # fabricated timestamp would sort as real data.
    await add_column(conn, "library_files", "fs_modified_at TIMESTAMP")
    await add_column(conn, "library_folders", "fs_modified_at TIMESTAMP")
