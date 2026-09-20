"""m167: api_keys.key_hash and key_prefix wide enough for what the code stores.

The columns were sized for a SHA-256 hex digest (64) and an 8-character prefix,
but ``generate_api_key`` has long stored a passlib pbkdf2-sha256 hash
(``$pbkdf2-sha256$29000$…``, 87 characters) and a ``bb_`` prefix of 11. SQLite
never enforces VARCHAR lengths, so nobody noticed; PostgreSQL does, so every
external-PostgreSQL install could not create an API key, and the SQLite →
PostgreSQL import died on the first key row with
``StringDataRightTruncationError: value too long for type character varying(64)``
(found 2026-09-07 while bringing a real farm's database onto the bundled
PostgreSQL).

Fresh installs get the widths from the model through ``create_all``; this is
the path an existing PostgreSQL database walks. On SQLite there is nothing to
change — the length is decoration there — and ``ALTER COLUMN TYPE`` does not
exist anyway.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres

version = 167
name = "api_key_column_widths"


async def upgrade(conn):
    if not is_postgres():
        return
    await conn.execute(text("ALTER TABLE api_keys ALTER COLUMN key_hash TYPE VARCHAR(255)"))
    await conn.execute(text("ALTER TABLE api_keys ALTER COLUMN key_prefix TYPE VARCHAR(16)"))
