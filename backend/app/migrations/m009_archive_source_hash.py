"""Track patched-file provenance on archives.

Adds two nullable columns to ``print_archives``:

    * ``source_content_hash VARCHAR(64)`` — SHA256 of the UNPATCHED source
      (library file or prior archive) from which this archive was dispatched.
      Populated only when BamDude dispatched the print AND the source is
      known. NULL for external prints (printer-screen / cloud / manual SD).
    * ``applied_patches TEXT`` — JSON array of patch identifiers that were
      applied to the source before upload, e.g.
      ``["vibration_fast_check_off"]``. Informational in v1; reprint
      semantics land later.

Dedup queries switch to ``COALESCE(source_content_hash, content_hash)`` so
BamDude-patched archives dedup against their library originals while
external prints keep their today-behaviour (dedup by raw content hash).

No backfill — old rows stay NULL and COALESCE falls back to ``content_hash``.
"""

from backend.app.migrations.helpers import add_column

version = 9
name = "archive_source_hash"


async def upgrade(conn):
    await add_column(conn, "print_archives", "source_content_hash VARCHAR(64)")
    await add_column(conn, "print_archives", "applied_patches TEXT")
