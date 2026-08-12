"""Mark the OIDC provider that ``BAMDUDE_OIDC_*`` owns (#2593).

A declarative deployment — compose, Helm, GitOps — has no way to click through
the settings UI, so one provider can now be declared entirely from the
environment. That row is rewritten on every boot, which means the UI must show
it read-only and the API must refuse writes to it: an edit would be silently
reverted at the next restart, which is worse than being told no.

This column is what makes that answerable. Without it nothing distinguishes the
env-declared provider from one an admin created by hand, and the lock would have
to be re-derived from the environment on every request.

Defaults to false, so every existing provider stays exactly as editable as it
was. Nothing is backfilled: on the next boot ``apply_env_oidc_provider`` sets
the flag on whichever row the environment names, and clears it on any other row
still carrying it.
"""

from backend.app.migrations.helpers import add_column

version = 130
name = "oidc_env_managed"


async def upgrade(conn):
    # BOOLEAN DEFAULT 0 — helpers translate the literal for PostgreSQL, which
    # rejects an integer default on a boolean column.
    await add_column(conn, "oidc_providers", "is_env_managed BOOLEAN DEFAULT 0")
