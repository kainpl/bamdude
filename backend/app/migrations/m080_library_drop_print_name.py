"""Drop the embedded 3MF Title (``print_name``) from library file metadata (#1489).

Library files stored the 3MF's ``<metadata name="Title">`` as
``file_metadata.print_name`` — generic ("Exported 3D Model") for Bambu Studio
exports, a marketing title for MakerWorld downloads — and the FileManager
wrongly preferred it over the filename for the card label, search and sort.
New imports no longer store it (see ``_without_print_name`` in
``api/routes/library.py``); this clears it from rows imported before the fix so
existing libraries don't need a per-file rename round-trip.

``file_metadata`` is a JSON (not JSONB) column. Idempotent — rows without the
key are untouched. Port of upstream Bambuddy 71e58e6c.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres

version = 80
name = "library_drop_print_name"


async def upgrade(conn):
    if is_postgres():
        # Cast to jsonb for the key-exists test (jsonb_exists avoids the `?`
        # operator, which clashes with driver parameter syntax) and the
        # `- key` removal, then back to json.
        await conn.execute(
            text(
                "UPDATE library_files SET file_metadata = (file_metadata::jsonb - 'print_name')::json "
                "WHERE jsonb_exists(file_metadata::jsonb, 'print_name')"
            )
        )
    else:
        await conn.execute(
            text(
                "UPDATE library_files SET file_metadata = json_remove(file_metadata, '$.print_name') "
                "WHERE json_extract(file_metadata, '$.print_name') IS NOT NULL"
            )
        )
