"""Add ``oidc_providers.default_group_id`` (upstream Bambuddy #1173).

Operator-configurable default group for auto-created OIDC users — when
``OIDCProvider.auto_create_users`` is True and the SSO callback creates a
new local user, the user lands in this group instead of the hard-coded
``"Viewers"`` fallback. Useful when an operator runs an isolated tenant
where the read-only default doesn't match the policy (an internal-only
SSO might want every new user in ``"Operators"``; a public-facing IdP
might want everyone in a more restrictive custom group).

Falls back to ``"Viewers"`` at runtime when:
  - the column is NULL (operator hasn't configured it);
  - the referenced group was deleted on SQLite (no FK enforcement) and
    the row has a dangling id.

Postgres declares the FK with ``ON DELETE SET NULL`` so a group delete
clears the ref atomically. SQLite ALTER TABLE ADD COLUMN can't carry an
FK constraint and ``PRAGMA foreign_keys`` is off by default anyway, so
the runtime resolution in ``mfa.py::oidc_callback`` re-checks via a
``select(Group).where(Group.id == provider.default_group_id)`` lookup
and falls through to Viewers when the group doesn't exist.

Idempotent — ``add_column`` is a no-op when the column already exists.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column

version = 51
name = "oidc_default_group"


async def upgrade(conn):
    if is_postgres():
        await add_column(
            conn,
            "oidc_providers",
            "default_group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL",
        )
    else:
        await add_column(conn, "oidc_providers", "default_group_id INTEGER")

    # Index supports the per-create / per-update lookup that resolves
    # the configured group, plus the auto-fill on the OIDC callback path.
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_oidc_providers_default_group_id ON oidc_providers (default_group_id)")
    )
