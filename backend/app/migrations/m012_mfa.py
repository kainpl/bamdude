"""MFA / 2FA / OIDC cluster schema (§18, upstream PR #933).

Ships the tables + column that back the 2FA feature set, OIDC SSO, JWT
revocation, and login rate-limiting. Safe for fresh installs (tables
are created via ``Base.metadata.create_all`` in ``init_db``) — this
migration exists to handle existing installs where ``create_all`` has
already run once without these models in the metadata registry.

New tables
----------
- ``user_totp`` (§18.1): TOTP authenticator-app secret + backup-code
  hashes + replay-protection counter per user.
- ``user_otp_codes`` (§18.1): email OTP codes with expiry + attempts
  + used flag.
- ``auth_ephemeral_tokens`` (§18.1 / §18.2 / §18.4): pre-auth,
  oidc_state, oidc_exchange, password_reset, email_otp_setup, slicer
  downloads, camera stream tokens, and revoked JTIs — all share this
  single short-lived-token table discriminated by ``token_type``.
- ``auth_rate_limit_events`` (§18.5): sliding-window rate-limit ledger
  for 2FA attempts + email sends + login attempts / IP rate-limit.
- ``oidc_providers`` (§18.2): OIDC provider configs (issuer URL,
  client id + encrypted secret, display name, icon, auto-link
  toggle).
- ``user_oidc_links`` (§18.2): links a local user to an OIDC subject
  identifier; unique per (provider_id, provider_user_id) and per
  (user_id, provider_id).

New column
----------
- ``users.password_changed_at DATETIME NULL`` — JWTs issued before
  this timestamp are rejected as stale (see ``_is_token_fresh`` in
  core/auth.py, §18.4 I2). NULL = never changed / legacy row, treated
  as "no freshness floor" by the freshness check.

No seed — all tables start empty; users opt into 2FA / OIDC themselves.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column, table_exists

version = 12
name = "mfa"


async def upgrade(conn):
    # The column is the only ALTER — every other table is a fresh
    # create_all target, so we only need to guard against an existing
    # column on an already-upgraded-by-create_all install.
    await add_column(conn, "users", "password_changed_at DATETIME")

    # Helpful indexes beyond the @index=True on the ORM side. Keep names short so
    # both sqlite and postgres identifier-length limits agree.
    for ddl in [
        # AuthEphemeralToken sweeps by (token_type, expires_at) during cleanup.
        "CREATE INDEX IF NOT EXISTS ix_auth_eph_type_exp ON auth_ephemeral_tokens(token_type, expires_at)",
        # Rate-limit sliding window reads by (username, event_type, occurred_at)
        # and by (username, event_type, occurred_at) with IP field; a combined
        # index on (event_type, occurred_at) handles the IP-wide buckets too.
        "CREATE INDEX IF NOT EXISTS ix_auth_rl_type_time ON auth_rate_limit_events(event_type, occurred_at)",
    ]:
        # Create-if-missing guard: some of the target tables might not exist
        # yet on a VERY old install that hasn't run create_all with the new
        # models imported. In that case the index DDL would fail; the tables
        # get created later by init_db() → create_all, and the index is
        # declared via the `index=True` columns there.
        table = ddl.split(" ON ")[1].split("(")[0].strip()
        if await table_exists(conn, table):
            await conn.execute(text(ddl))


async def seed(session_factory):  # pragma: no cover — no-op, tables are self-starting
    async with session_factory() as db:
        _ = db  # noqa: ARG001
