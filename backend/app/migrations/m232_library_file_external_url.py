"""A link of the operator's own on a library file (upstream 12dddada0, #3128).

``library_files.external_url VARCHAR(2048)`` — a web address the operator
attaches to a file by hand (a Printables page, a datasheet): nullable, never
fetched by the server, http/https only (the route validates; the column only
bounds the length). It sits beside ``source_url`` (m033), which stays what it
was: provenance written by the MakerWorld import and the slicer, indexed for
their dedup, never edited by hand.

No backfill — no existing file has such a link — and no dependency on any other
column of the table.
"""

from backend.app.migrations.helpers import add_column

version = 232
name = "library_file_external_url"


async def upgrade(conn):
    await add_column(conn, "library_files", "external_url VARCHAR(2048)")
