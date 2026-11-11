"""Carry an explicit Remember Me choice through ephemeral auth handoffs.

NULL preserves the legacy/local policy for existing rows. In particular this
migration must not infer a checked preference from token expiry or backfill
OIDC state/exchange rows. Runtime consumers interpret legacy OIDC NULL as false;
new issuers store their explicit choice. No server default may erase that
distinction on existing or future legacy-compatible inserts.
"""

from backend.app.migrations.helpers import add_column

version = 220
name = "auth_remember_me"


async def upgrade(conn):
    await add_column(conn, "auth_ephemeral_tokens", "remember_me BOOLEAN")
