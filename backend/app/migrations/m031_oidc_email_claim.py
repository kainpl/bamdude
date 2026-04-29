"""Add OIDC ``email_claim`` + ``require_email_verified`` for B.16 + A.31 + A.32 (#1126).

Two new columns let operators point at Azure Entra ID's ``preferred_username``
or ``upn`` claim instead of the standard ``email`` (Azure never sends
``email_verified``), and skip the verified-email gate when the IdP genuinely
doesn't surface that claim. The defaults preserve existing
PocketID/Authentik/Keycloak/Authelia/Google behaviour exactly.

Defense-in-depth: on PostgreSQL we add a CHECK constraint mirroring the
application-level ``_enforce_auto_link_safety`` guard so a direct DB INSERT
that bypasses the API can't land the unsafe combo. SQLite installs rely on
the application-level guard alone (adding a CHECK constraint to an existing
SQLite table requires a full table rewrite — disproportionate for what is
already a redundant safety belt).
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column

version = 31
name = "oidc_email_claim"

_CONSTRAINT_NAME = "ck_auto_link_requires_verified_email_claim"
_CHECK_FORMULA = "auto_link_existing_accounts = FALSE OR email_claim != 'email' OR require_email_verified = TRUE"


async def upgrade(conn):
    await add_column(conn, "oidc_providers", "email_claim VARCHAR(64) NOT NULL DEFAULT 'email'")
    if is_postgres():
        await add_column(conn, "oidc_providers", "require_email_verified BOOLEAN NOT NULL DEFAULT TRUE")
        # Idempotent: drop-and-add so reruns end up with the same final state
        # regardless of whether a previous attempt succeeded partway.
        await conn.execute(text(f"ALTER TABLE oidc_providers DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}"))
        await conn.execute(
            text(f"ALTER TABLE oidc_providers ADD CONSTRAINT {_CONSTRAINT_NAME} CHECK ({_CHECK_FORMULA})")
        )
    else:
        await add_column(conn, "oidc_providers", "require_email_verified BOOLEAN NOT NULL DEFAULT 1")


async def seed(session_factory):  # pragma: no cover — no-op
    async with session_factory() as db:
        _ = db  # noqa: ARG001
