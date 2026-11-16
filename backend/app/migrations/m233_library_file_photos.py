"""Photos on a library file (upstream 12dddada0, #3128).

``library_file_photos`` — one row per photo an operator attached to a library
file: the name its bytes are stored under in ``<library>/photos/<file id>/``,
the client's name for display, what was stored (type, size, pixels), an
optional caption, who added it (id + a name snapshot) and when.

Upstream keeps a JSON list of file names on the file row and stores the bytes
as sent. Here a photo is a row of its own — a caption, an author and a stable
id for its URL — and ``services/library_file_photos.py`` is the only writer of
the table and of the folder.

``AUTOINCREMENT`` on SQLite: a photo's id is in the URL a browser caches, and
SQLite would otherwise give a deleted photo's id to the next one. The foreign
keys are declared for PostgreSQL; SQLite runs no FK actions, so the rows are
removed in code wherever a library file is purged, and a deleted user is
detached in code.

Depends on no other column of ``library_files`` than its ``id``.
"""

from backend.app.migrations.helpers import table_exists

version = 233
name = "library_file_photos"


async def upgrade(conn):
    sqlite = conn.dialect.name == "sqlite"
    if not await table_exists(conn, "library_file_photos"):
        pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
        ts = "DATETIME" if sqlite else "TIMESTAMP"
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE library_file_photos (
                id {pk},
                library_file_id INTEGER NOT NULL REFERENCES library_files(id) ON DELETE CASCADE,
                stored_name VARCHAR(64) NOT NULL,
                original_name VARCHAR(255),
                content_type VARCHAR(32) NOT NULL,
                file_size INTEGER NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                caption VARCHAR(500),
                uploaded_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                uploaded_by_name VARCHAR(150),
                created_at {ts} NOT NULL
            )
            """
        )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_library_file_photos_library_file_id ON library_file_photos (library_file_id)"
    )
