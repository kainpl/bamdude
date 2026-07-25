"""Backfill the ``makerworld:*`` permissions onto existing system groups.

``makerworld:view`` / ``makerworld:import`` were added to the fresh-install
``DEFAULT_GROUPS`` lists (``core/permissions.py`` — Operators get both, Viewers
get view) but **never to a migration**, so they only ever reached databases
created after that change. On every DB seeded earlier — which includes every
Bambuddy database imported into BamDude — an Operator or Viewer hits a 403 on
MakerWorld browse and import, because ``Group.permissions`` is a stored list and
nothing re-syncs it at startup (we deliberately don't self-heal groups; see the
frozen-migrations rule).

Administrators are masked from the symptom by the ``is_admin`` short-circuit in
the permission check, but the row is still granted here so the stored list stays
truthful and the O2 discipline ("every Permission is seeded to Administrators in
its migration") holds for these two as well.

Found by the 2026-07-25 back-audit sweep of previously-closed audit claims —
recorded against row O2 of ``temp/upstream-audits/0.2.4.8-0.2.4.9.md``, though
the permissions themselves belong to the MakerWorld port cycle.

Schema-neutral: no DDL, seed only.
"""

version = 111
name = "makerworld_permission_backfill"


async def upgrade(conn):
    """No schema change — this migration only re-grants stored permissions."""
    return


async def seed(session_factory):
    """Grant ``makerworld:*`` to the system groups that should already have it.

    Column-explicit read + Core update (see feedback_migration_seed_columns): an
    entity-wide ``select(Group)`` would emit not-yet-existing columns and break
    an upgrade chain where this migration runs before a later column lands.

    Custom (non-system) groups are left alone — an operator who built their own
    group decides its permissions, and silently widening it would be worse than
    the 403.
    """
    from sqlalchemy import select, update

    from backend.app.models.group import Group

    view, imp = "makerworld:view", "makerworld:import"

    async with session_factory() as db:
        result = await db.execute(select(Group.id, Group.name, Group.is_system, Group.permissions))
        dirty = 0
        for row in result.all():
            if not row.is_system:
                continue
            if row.name in ("Administrators", "Operators"):
                wanted = [view, imp]
            elif row.name == "Viewers":
                wanted = [view]
            else:
                continue

            perms = list(row.permissions or [])
            to_add = [p for p in wanted if p not in perms]
            if to_add:
                await db.execute(update(Group).where(Group.id == row.id).values(permissions=perms + to_add))
                dirty += 1
        if dirty:
            await db.commit()
