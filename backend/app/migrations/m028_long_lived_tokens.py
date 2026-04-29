"""Add `long_lived_tokens` table for B.10 (#1108).

Per-user, hashed-at-rest, named, revocable camera-stream tokens with a
maximum 365-day TTL (issue #1108: ``expire_in: 0 = never`` was rejected).
The table sits next to ``auth_ephemeral_tokens`` (60-min reusable) and
``api_keys`` (global webhook tokens) — see the model docstring for the
"why three tables" rationale.

Idempotent on installs that already have the table because we use
``Base.metadata.create_all()`` semantics — but legacy installs still need
this migration to run so ``init_db()`` records the version in
``_migrations`` and the bootstrap logic doesn't loop.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import table_exists

version = 28
name = "long_lived_tokens"


async def upgrade(conn):
    if await table_exists(conn, "long_lived_tokens"):
        return
    if is_postgres():
        await conn.execute(
            text(
                """
                CREATE TABLE long_lived_tokens (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name VARCHAR(100) NOT NULL,
                    lookup_prefix VARCHAR(8) NOT NULL,
                    secret_hash VARCHAR(255) NOT NULL,
                    scope VARCHAR(32) NOT NULL DEFAULT 'camera_stream',
                    expires_at TIMESTAMP NOT NULL,
                    last_used_at TIMESTAMP,
                    revoked_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT now()
                )
                """
            )
        )
    else:
        await conn.execute(
            text(
                """
                CREATE TABLE long_lived_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name VARCHAR(100) NOT NULL,
                    lookup_prefix VARCHAR(8) NOT NULL,
                    secret_hash VARCHAR(255) NOT NULL,
                    scope VARCHAR(32) NOT NULL DEFAULT 'camera_stream',
                    expires_at DATETIME NOT NULL,
                    last_used_at DATETIME,
                    revoked_at DATETIME,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
        )
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_long_lived_tokens_user_id ON long_lived_tokens(user_id)"))
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_long_lived_tokens_lookup_prefix ON long_lived_tokens(lookup_prefix)")
    )


async def seed(session_factory):  # pragma: no cover — no-op
    async with session_factory() as db:
        _ = db  # noqa: ARG001
