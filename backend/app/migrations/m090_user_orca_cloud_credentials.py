"""Add per-user Orca Cloud credential columns to ``users``.

Mirrors the Bambu Cloud columns (``cloud_token`` etc.) but adds a refresh
token + expiry — Orca uses OrcaSlicer 2.4.0-alpha's Supabase PKCE flow, which
issues short-lived (1h) access tokens with rotating single-use refresh tokens
— plus three transient PKCE-handshake columns held between
``/orca-cloud/auth/start`` and ``/orca-cloud/auth/finish`` (10-min TTL).

Fresh installs get the columns from the model's ``create_all``; this backfills
existing DBs. ``DATETIME`` is SQLite-only — Postgres uses ``TIMESTAMP`` — so
the two datetime columns are dialect-branched per project convention (the
``add_column`` helper doesn't translate column types).

Upstream Bambuddy #<orca-cloud> / commit ``18d534c9`` (which used an inline
``run_migrations`` step; BamDude uses numbered migrations).
"""

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import add_column

version = 90
name = "user_orca_cloud_credentials"


async def upgrade(conn):
    datetime_type = "DATETIME" if is_sqlite() else "TIMESTAMP"

    await add_column(conn, "users", "orca_cloud_token VARCHAR(2000)")
    await add_column(conn, "users", "orca_cloud_refresh_token VARCHAR(128)")
    await add_column(conn, "users", f"orca_cloud_expires_at {datetime_type}")
    await add_column(conn, "users", "orca_cloud_email VARCHAR(255)")
    await add_column(conn, "users", "orca_cloud_user_id VARCHAR(64)")
    await add_column(conn, "users", "orca_cloud_pending_verifier VARCHAR(64)")
    await add_column(conn, "users", "orca_cloud_pending_state VARCHAR(32)")
    await add_column(conn, "users", f"orca_cloud_pending_at {datetime_type}")
