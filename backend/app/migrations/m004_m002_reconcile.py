"""Reconcile schema for installs that ran an earlier version of m002.

Background
----------
m002 (``m002_bamdude_311``) was amended several times after its initial
release: new ``add_column`` calls, new tables, and new data normalizations
kept accreting as features shipped (swap mode, macros, queue rework,
REST smart plugs, LDAP, energy snapshots, library notes, etc.). The
migration runner records a migration as "applied" by version number only,
so any install that ran an early m002 has ``version=2`` in its
``_migrations`` table even though subsequent m002 amendments never ran on
that database.

Symptom: stuck installs boot and crash with ``no such column:
printers.require_plate_clear`` (or any of the other post-release
additions) because the ORM models expect the current schema while the
database is frozen in the m002-at-checkout-time state.

Going forward we treat **released migration files as frozen** - any
schema change ships as a new ``m00X+1`` file. This migration exists to
catch up every install that missed one of the m002 amendments.

Implementation
--------------
Re-runs ``m002.upgrade()`` verbatim. Every operation inside m002 is
idempotent or existence-guarded:

* ``add_column(table, col_def)`` → ``column_exists()`` short-circuits if
  the column is already present.
* ``recreate_table(...)`` is wrapped in ``if await column_exists(old_col)``
  - only fires while the obsolete column still exists.
* ``CREATE TABLE`` is either ``IF NOT EXISTS`` or wrapped in
  ``if not await table_exists()``.
* ``CREATE INDEX IF NOT EXISTS``.
* ``UPDATE ... SET x = y WHERE ...`` is set-of-values, so repeat runs are
  no-ops once data has been normalized.

For a stuck install this fills in the missing columns / tables. For a
fully-caught-up install (including fresh ``create_all()`` schemas) it is
a no-op: every guard trips short and nothing executes.

The seed phase of m002 is intentionally **not** replayed - seeds perform
``INSERT`` operations that are not always safe to repeat. Stuck installs
that ran m002's original seed already have the seeded rows.
"""

from __future__ import annotations

version = 4
name = "m002_reconcile"


async def upgrade(conn):
    """Replay m002.upgrade() - idempotent for every code path."""
    from backend.app.migrations.m002_bamdude_311 import upgrade as m002_upgrade

    await m002_upgrade(conn)
