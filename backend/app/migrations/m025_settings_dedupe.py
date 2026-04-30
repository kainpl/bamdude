"""Defensive dedupe of `settings` rows by `key`, plus unique-index guarantee.

Why
---
``Settings.key`` is declared ``unique=True`` in the ORM, but
``Base.metadata.create_all`` only creates objects that don't already exist.
On installs that came from a very old Bambuddy import the legacy table can
exist *without* the unique constraint — and any subsequent ``INSERT OR
IGNORE`` (or upsert that relied on ON CONFLICT (key)) would silently
degrade to a plain INSERT, leaving duplicate rows by ``key``. The first
duplicate also blocks the later attempt to add the constraint.

What this does
--------------
1. Picks the canonical row per key as ``MIN(id)`` (oldest wins) and deletes
   the rest. Idempotent on installs that already enforce the constraint
   (zero rows match).
2. Creates ``ix_settings_key`` as ``UNIQUE`` if it isn't already present —
   the index name matches what SQLAlchemy generates from the
   ``index=True, unique=True`` mapped_column declaration.

Both steps are no-ops on a fresh DB or any DB upgraded post-fork; the
migration is recorded in ``_migrations`` so it never runs twice on the
same install.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres

version = 25
name = "settings_dedupe"


async def upgrade(conn):
    # 1. Drop duplicate settings rows, keeping MIN(id) per key. Safe even
    # when the table already enforces the constraint — the subquery returns
    # zero rows in that case.
    if is_postgres():
        await conn.execute(text("DELETE FROM settings WHERE id NOT IN (SELECT MIN(id) FROM settings GROUP BY key)"))
    else:
        await conn.execute(text("DELETE FROM settings WHERE id NOT IN (SELECT MIN(id) FROM settings GROUP BY key)"))

    # 2. Ensure the unique index exists. Both engines support
    # ``CREATE UNIQUE INDEX IF NOT EXISTS`` so this is a single statement.
    # Index name matches SQLAlchemy's auto-generated name for
    # ``mapped_column(..., unique=True, index=True)``.
    await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_settings_key ON settings(key)"))


async def seed(session_factory):  # pragma: no cover — no-op
    async with session_factory() as db:
        _ = db  # noqa: ARG001
