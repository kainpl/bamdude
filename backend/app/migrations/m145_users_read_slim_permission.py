"""Seed ``users:read_slim`` into the system groups.

``GET /users/slim`` returns ``{id, username}`` and nothing else, so an operator
— or an API key — can turn the ``created_by_id`` values archives, stats and the
queue already hand back into names. Before it, the only user listing was the
administrative one, so somebody granted ``stats:filter_by_user`` but not
``users:read`` got an empty filter with no indication why.

⚠️ **Administrators are seeded explicitly, not self-healed.** Fresh installs get
the permission because that group is defined as ``ALL_PERMISSIONS``, but an
existing database holds a stored list that nothing re-derives at startup — so a
new ``Permission`` that never lands in a migration is simply missing forever on
every upgraded install. That is the O2 discipline, and it is why this file
exists at all for a permission the default-group table already covers.

Operators get it too: they hold ``stats:filter_by_user``, which is what makes
the id→name mapping worth having. Viewers do not — they can read stats but not
filter them by user, so the listing would answer a question they cannot ask.

No schema change: permissions are a JSON list on ``groups``.
"""

import logging

logger = logging.getLogger(__name__)

version = 145
name = "users_read_slim_permission"

NEW_PERMISSIONS = ["users:read_slim"]


async def upgrade(conn):
    """Nothing to do — this migration is DML only, in :func:`seed`."""


async def seed(session_factory):
    """Grant ``users:read_slim`` to Administrators and Operators.

    Column-explicit read plus a Core update (see the seed discipline): an
    entity-wide ``select(Group)`` would emit columns a later migration has not
    added yet and break an upgrade chain that runs this migration first.
    """
    from sqlalchemy import select, update

    from backend.app.models.group import Group

    async with session_factory() as db:
        result = await db.execute(select(Group.id, Group.name, Group.is_system, Group.permissions))
        dirty = 0
        for row in result.all():
            if not (row.is_system and row.name in ("Administrators", "Operators")):
                continue

            existing = set(row.permissions or [])
            to_add = [p for p in NEW_PERMISSIONS if p not in existing]
            if not to_add:
                continue
            await db.execute(
                update(Group).where(Group.id == row.id).values(permissions=list(row.permissions or []) + to_add)
            )
            dirty += 1

        if dirty:
            await db.commit()
            logger.info("m145: seeded users:read_slim into %d group(s)", dirty)
