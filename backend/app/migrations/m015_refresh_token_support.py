"""Refresh-token support for sliding-session auth (§18.14).

Adds two nullable columns to ``auth_ephemeral_tokens``:

* ``used_at DATETIME`` — set by /auth/refresh the first time a refresh
  token is consumed. A subsequent request carrying the same (now non-NULL
  ``used_at``) token is treated as a replay / stolen-cookie attack and the
  whole family is revoked. Race-proof consumption uses
  ``UPDATE … WHERE used_at IS NULL`` so two concurrent refreshes can't
  both win — exactly one flips the flag, the loser falls through to the
  reuse path.
* ``family_id VARCHAR(32)`` — common id for every rotation descended from
  one /login. Reuse detection revokes all siblings at once; ``DELETE WHERE
  family_id = ?`` is the hot path, so the column is indexed.

Backfill: every existing row predates sliding-session (they're pre_auth /
oidc_state / oidc_exchange / password_reset / email_otp_setup / slicer /
camera / revoked_jti), so both columns stay NULL. Only rows created by
``AuthEphemeralToken.new_refresh`` ever populate them.

Existing ``token_type`` column is a String(20) already — new value
``"refresh"`` fits without a schema bump. The previous entries' values
stay untouched.
"""

from backend.app.migrations.helpers import add_column

version = 15
name = "refresh_token_support"


async def upgrade(conn):
    await add_column(conn, "auth_ephemeral_tokens", "used_at DATETIME")
    await add_column(conn, "auth_ephemeral_tokens", "family_id VARCHAR(32)")


async def seed(session_factory):  # pragma: no cover — no-op
    async with session_factory() as db:
        _ = db  # noqa: ARG001
