"""Per-user in-app inbox: ``user_notifications`` + ``users.inbox_events`` + the ``notifications:inbox`` permission.

Spec: vault 60-specs/notification-center-spec §4. The inbox is a fan-out on write —
one row per subscribed user per event — so reads and the unread counter are one
indexed query per user. ``users.inbox_events`` mirrors ``telegram_chats.notify_events``
(NULL = defaults). The permission is seeded to all three system groups: an inbox
is a person's own view of the farm, not an administrative surface (O2 discipline —
Administrators are not self-healed at startup).
"""

import logging

from sqlalchemy import select, update

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import add_column, json_column_type, table_exists

logger = logging.getLogger(__name__)

version = 176
name = "user_inbox"

NEW_PERMISSIONS = ["notifications:inbox"]
SEEDED_GROUPS = ("Administrators", "Operators", "Viewers")


async def upgrade(conn):
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    stamp = "DATETIME" if sqlite else "TIMESTAMP"

    if not await table_exists(conn, "user_notifications"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE user_notifications (
                id {pk},
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                event_type VARCHAR(50) NOT NULL,
                severity VARCHAR(10) NOT NULL,
                title VARCHAR(255) NOT NULL,
                message TEXT NOT NULL,
                printer_id INTEGER,
                printer_name VARCHAR(100),
                extra_data {json_column_type()},
                created_at {stamp} NOT NULL,
                read_at {stamp}
            )
            """
        )

    # Outside the table guard: a fresh install has the table from create_all
    # and needs the indexes all the same.
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_user_notifications_user_read ON user_notifications (user_id, read_at)"
    )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_user_notifications_user_created ON user_notifications (user_id, created_at)"
    )

    await add_column(conn, "users", f"inbox_events {json_column_type()}")


async def seed(session_factory):
    """Grant ``notifications:inbox`` to the three system groups, append-only, idempotent (m145 shape)."""
    from backend.app.models.group import Group

    async with session_factory() as db:
        result = await db.execute(select(Group.id, Group.name, Group.is_system, Group.permissions))
        dirty = 0
        for row in result.all():
            if not (row.is_system and row.name in SEEDED_GROUPS):
                continue
            existing = set(row.permissions or [])
            to_add = [p for p in NEW_PERMISSIONS if p not in existing]
            if not to_add:
                continue
            await db.execute(
                update(Group.__table__)
                .where(Group.__table__.c.id == row.id)
                .values(permissions=list(row.permissions or []) + to_add)
            )
            dirty += 1
        if dirty:
            await db.commit()
            logger.info("m176: seeded notifications:inbox into %d group(s)", dirty)
