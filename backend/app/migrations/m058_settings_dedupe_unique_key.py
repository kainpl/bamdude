"""Dedupe legacy ``settings`` rows + ensure UNIQUE(key) index.

Our ``Settings`` model has ``unique=True, index=True`` on ``key`` since the
fork point (4954df1), so any fresh BamDude install gets the constraint
via ``Base.metadata.create_all`` and never sees duplicates. This migration
is a **safety net for legacy DBs** — specifically operators who imported
a pre-fork Bambuddy SQLite file that was created without the UNIQUE
constraint and accumulated duplicate rows over restarts (upstream's
``INSERT OR IGNORE`` silently degrades to plain INSERT when the index is
absent, so each `systemctl restart` adds another `advanced_auth_enabled`
/ `smtp_auth_enabled` row, and `scalar_one_or_none()` eventually blows
up with ``MultipleResultsFound``).

Two idempotent steps:

1. ``DELETE FROM settings WHERE id NOT IN (SELECT MIN(id) FROM settings GROUP BY key)``
   — keeps the *lowest-id* row per key. Choice mirrors upstream's: the
   first-written row is most likely the seed default, the later
   duplicates are noise from restart loops. Operators who *intended* a
   particular value can re-set it via the UI / API after upgrade.
2. ``CREATE UNIQUE INDEX IF NOT EXISTS ix_settings_key ON settings(key)``
   — creates the index iff it's not already there. Fresh installs and
   Postgres no-op out.

Adapted from upstream Bambuddy ``b99ceb26``.
"""

from sqlalchemy import text

version = 58
name = "settings_dedupe_unique_key"


async def upgrade(conn):
    # Dedupe first — CREATE UNIQUE INDEX would error if dupes are still present.
    await conn.execute(text("DELETE FROM settings WHERE id NOT IN (SELECT MIN(id) FROM settings GROUP BY key)"))
    # Add the missing unique index. The IF NOT EXISTS clause makes this a
    # no-op on fresh installs (which already have it from create_all) and
    # on the legacy installs after they've run this migration once.
    await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_settings_key ON settings(key)"))
