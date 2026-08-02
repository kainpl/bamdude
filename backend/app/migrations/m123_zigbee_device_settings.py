"""Per-device Zigbee settings, and a sensor the operator can name.

Two tables, and the split between them is the point.

``zigbee_devices`` is what the radio knows: one row per paired IEEE, created
when the device pairs. It holds the reporting parameters and the poll/staleness
overrides for BOTH device classes. They cannot live on ``smart_plugs`` — that
table also carries Tasmota, Home Assistant, MQTT and REST plugs, for which these
columns mean nothing — and they must exist before the entity row does, since a
plug is paired first and added afterwards while already being configured.

``smart_sensors`` is what the farm does with a sensor: the operator's own name
and a location string, mirroring ``smart_plugs``. Its existence IS adoption;
there is deliberately no ``adopted`` flag anywhere, because a plug's adoption
has always meant "a ``smart_plugs`` row references this IEEE" and a second
boolean would be a second source of truth for the same question.

Nothing is back-filled here: the migration has no radio and cannot know which
devices are paired. ``main.py`` reconciles the table against the coordinator on
the first startup after this lands.

The four ``smart_sensors:*`` permissions are seeded to the system groups below.
Administrators are not self-healed at startup and our migrations are frozen, so
a permission not seeded here is a permission nobody ever has.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import table_exists

logger = logging.getLogger(__name__)

version = 123
name = "zigbee_device_settings"

NEW_PERMISSIONS = [
    "smart_sensors:read",
    "smart_sensors:create",
    "smart_sensors:update",
    "smart_sensors:delete",
]
VIEWER_PERMISSIONS = ["smart_sensors:read"]


async def upgrade(conn):
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME" if sqlite else "TIMESTAMP"
    # SQLAlchemy's JSON type is TEXT on SQLite and JSON on PostgreSQL. Matching
    # it here rather than reaching for JSONB keeps the migrated schema identical
    # to the one ``create_all`` produces on a fresh install — a difference the
    # portable-backup export would otherwise carry across silently.
    json_type = "TEXT" if sqlite else "JSON"

    if not await table_exists(conn, "zigbee_devices"):
        await conn.execute(
            text(
                f"""
                CREATE TABLE zigbee_devices (
                    ieee VARCHAR(23) NOT NULL PRIMARY KEY,
                    kind VARCHAR(10) NOT NULL,
                    name VARCHAR(100),
                    reporting {json_type},
                    poll_seconds INTEGER,
                    stale_after_seconds INTEGER,
                    first_seen_at {ts} DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at {ts} DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        logger.info("m123: created zigbee_devices")

    if not await table_exists(conn, "smart_sensors"):
        await conn.execute(
            text(
                f"""
                CREATE TABLE smart_sensors (
                    id {pk},
                    name VARCHAR(100) NOT NULL,
                    location VARCHAR(100),
                    zigbee_ieee VARCHAR(23) NOT NULL UNIQUE,
                    created_at {ts} DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        logger.info("m123: created smart_sensors")

    await conn.execute(
        text("CREATE UNIQUE INDEX IF NOT EXISTS ix_smart_sensors_zigbee_ieee ON smart_sensors (zigbee_ieee)")
    )


async def seed(session_factory):
    """Grant smart_sensors:* to the system groups.

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
            existing = set(row.permissions or [])
            if row.is_system and row.name in ("Administrators", "Operators"):
                wanted = NEW_PERMISSIONS
            elif row.is_system and row.name == "Viewers":
                wanted = VIEWER_PERMISSIONS
            else:
                continue

            to_add = [p for p in wanted if p not in existing]
            if not to_add:
                continue
            await db.execute(
                update(Group).where(Group.id == row.id).values(permissions=list(row.permissions or []) + to_add)
            )
            dirty += 1

        if dirty:
            await db.commit()
            logger.info("m123: seeded smart_sensors permissions into %d group(s)", dirty)
