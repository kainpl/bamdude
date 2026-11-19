"""The default Operators group may reprint any archive (``archives:reprint_all``).

Queueing a print from an archive and re-slicing one now ask the reprint right,
as Reprint itself always did. The default Operators held only
``archives:reprint_own``, and a print started from the printer's screen, a
slicer or the virtual printer has no owner — so operators could no longer queue
or re-slice those, nor a colleague's print, on a shared farm where they see
every print anyway (``archives:read_all``). The owner's ruling: the default
Operators group reprints any archive. Reading is not widened; update and delete
stay "own only".

Seeded to the system group named Operators only, append-only and idempotent
(the m176 / m193 shape): a renamed, deleted or custom group is the
administrator's to change, and Viewers get nothing. Administrators already hold
the permission. Fresh installs get it from ``core.permissions.DEFAULT_GROUPS``.
"""

import logging

from sqlalchemy import JSON, Boolean, Integer, String, column, select, table, update

logger = logging.getLogger(__name__)

version = 249
name = "operators_reprint_all"

NEW_PERMISSIONS = ["archives:reprint_all"]
SEEDED_GROUPS = ("Operators",)

# The columns as they are at this step — never the ORM model, which inherits later ones.
_groups = table(
    "groups",
    column("id", Integer),
    column("name", String),
    column("is_system", Boolean),
    column("permissions", JSON),
)


async def upgrade(conn):
    """No schema change."""


async def seed(session_factory):
    """Grant ``archives:reprint_all`` to the system Operators group, once."""
    async with session_factory() as db:
        result = await db.execute(select(_groups.c.id, _groups.c.name, _groups.c.is_system, _groups.c.permissions))
        dirty = 0
        for row in result.all():
            if not (row.is_system and row.name in SEEDED_GROUPS):
                continue
            current = list(row.permissions or [])
            to_add = [p for p in NEW_PERMISSIONS if p not in current]
            if not to_add:
                continue
            await db.execute(update(_groups).where(_groups.c.id == row.id).values(permissions=current + to_add))
            dirty += 1
        if dirty:
            await db.commit()
            logger.info("m249: seeded archives:reprint_all into %d group(s)", dirty)
