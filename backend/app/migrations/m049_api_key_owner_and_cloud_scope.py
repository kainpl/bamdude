"""Add ``api_keys.user_id`` + ``api_keys.can_access_cloud`` (upstream Bambuddy #1182).

API keys were previously global / ownerless, which meant cloud-aware routes
(slice, slicer-presets) had no way to look up "this key's user" so they could
borrow that user's per-user Bambu Cloud token. The upstream fix tags every
new key with the creating user's id and gates cloud spend behind an explicit
``can_access_cloud`` opt-in — keys without an owner cannot toggle the flag.

This migration is purely additive on existing rows:

- ``user_id`` is added nullable so legacy keys (and programmatically-created
  keys without a request user) keep working. Routes that need the cloud
  token treat ``user_id IS NULL`` as "no cloud access available". Indexed
  for the FK lookup ``app.api.routes.users::delete_user`` does on user
  deletion (SQLite has FK enforcement off by default, so the cascade is
  emulated in code; see that route for the explicit DELETE).
- ``can_access_cloud`` defaults to False so legacy keys cannot silently
  spend the owner's cloud token after upgrade — operators must flip it on
  per-key via the UI / PATCH endpoint, which also re-checks ownership.

Idempotent: ``add_column`` is a no-op when the column already exists.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column

version = 49
name = "api_key_owner_and_cloud_scope"


async def upgrade(conn):
    # SQLite ALTER TABLE ADD COLUMN cannot declare a FK reference, so the
    # column is added without a constraint and the application-level
    # cascade in routes/users.py provides the equivalent guarantee.
    if is_postgres():
        await add_column(
            conn,
            "api_keys",
            "user_id INTEGER REFERENCES users(id) ON DELETE CASCADE",
        )
    else:
        await add_column(conn, "api_keys", "user_id INTEGER")

    await add_column(conn, "api_keys", "can_access_cloud BOOLEAN DEFAULT 0 NOT NULL")

    # Index for the cascade-on-user-delete lookup.
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_api_keys_user_id ON api_keys (user_id)"))
