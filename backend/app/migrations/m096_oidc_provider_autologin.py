"""Add ``is_autologin`` to ``oidc_providers`` (#1589 / G8-H1).

Per-provider SSO-autologin flag: LoginPage redirects unauthenticated visitors straight
to the flagged provider on mount. At most one provider carries it (enforced in the app
layer at create/update). Fresh installs get the column from create_all; this backfills
existing DBs. PostgreSQL rejects ``DEFAULT 0`` for BOOLEAN — branch the literal
(SQLite ``0`` / PG ``FALSE``); add_column does not translate boolean defaults.
Idempotent + DEBUG-re-run-safe. Upstream Bambuddy #1589 / 70857af3 (inline there;
BamDude uses numbered migrations)."""

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column

version = 96
name = "oidc_provider_autologin"


async def upgrade(conn):
    false_literal = "FALSE" if is_postgres() else "0"
    await add_column(conn, "oidc_providers", f"is_autologin BOOLEAN DEFAULT {false_literal}")
