"""Direct-to-device label printing: devices, jobs and the cassette catalogue.

Three tables, one API-key scope column, four permissions.

``label_devices.enabled`` defaults FALSE and is never backfilled to TRUE — a
device is adopted by a person, not by having polled once. An API key proves the
caller is a bridge; it does not decide that the printer behind it should be
given our labels.

``api_keys.can_print_labels`` defaults FALSE, unlike the ``can_manage_*``
columns m104 backfilled per row. Those split a capability keys already had, so
silence would have taken something away; this one is new, so silence takes
nothing.

The four permissions are seeded here. Administrators are not self-healed at
startup and our migrations are frozen, so a permission not seeded here is a
permission nobody ever has. Operators get READ and JOBS_CREATE but **not**
MANAGE: adopting a device decides that a machine on somebody's desk may receive
our labels, which is an administrator's call.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, text, update

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import add_column, table_exists

logger = logging.getLogger(__name__)

version = 147
name = "device_direct_labels"

ADMIN_PERMISSIONS = [
    "label_devices:read",
    "label_devices:poll",
    "label_devices:manage",
    "label_jobs:create",
]
#: Print to a device and see which ones exist; adopting one is not theirs.
OPERATOR_PERMISSIONS = ["label_devices:read", "label_jobs:create"]
VIEWER_PERMISSIONS = ["label_devices:read"]


async def upgrade(conn):
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME" if sqlite else "TIMESTAMP"
    blob = "BLOB" if sqlite else "BYTEA"
    boolean = "BOOLEAN"

    if not await table_exists(conn, "label_devices"):
        await conn.execute(
            text(
                f"""
                CREATE TABLE label_devices (
                    id {pk},
                    installation_id VARCHAR(64) NOT NULL UNIQUE,
                    driver VARCHAR(32) NOT NULL DEFAULT 'niimbot',
                    model VARCHAR(64),
                    protocol_version INTEGER,
                    transport VARCHAR(16),
                    address VARCHAR(128),
                    name VARCHAR(128),
                    enabled {boolean} NOT NULL DEFAULT '0',
                    density INTEGER NOT NULL DEFAULT 3,
                    app_version VARCHAR(32),
                    last_seen_at {ts},
                    cassette_barcode VARCHAR(64),
                    cassette_width_mm FLOAT,
                    cassette_height_mm FLOAT,
                    paper_state INTEGER,
                    power_level INTEGER,
                    printer_reachable {boolean} NOT NULL DEFAULT '0',
                    created_at {ts}
                )
                """
            )
        )
        await conn.execute(text("CREATE INDEX ix_label_devices_installation_id ON label_devices (installation_id)"))
        logger.info("m147: created label_devices")

    if not await table_exists(conn, "label_jobs"):
        await conn.execute(
            text(
                f"""
                CREATE TABLE label_jobs (
                    id {pk},
                    device_id INTEGER NOT NULL REFERENCES label_devices(id) ON DELETE CASCADE,
                    spool_id INTEGER,
                    template_id INTEGER,
                    width_mm FLOAT NOT NULL,
                    height_mm FLOAT NOT NULL,
                    copies INTEGER NOT NULL DEFAULT 1,
                    image_png {blob} NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    claimed_at {ts},
                    created_by INTEGER,
                    created_at {ts}
                )
                """
            )
        )
        await conn.execute(text("CREATE INDEX ix_label_jobs_device_id ON label_jobs (device_id)"))
        await conn.execute(text("CREATE INDEX ix_label_jobs_status ON label_jobs (status)"))
        logger.info("m147: created label_jobs")

    if not await table_exists(conn, "label_cassettes"):
        await conn.execute(
            text(
                f"""
                CREATE TABLE label_cassettes (
                    id {pk},
                    barcode VARCHAR(64) NOT NULL UNIQUE,
                    width_mm FLOAT NOT NULL,
                    height_mm FLOAT NOT NULL,
                    name VARCHAR(128),
                    created_at {ts}
                )
                """
            )
        )
        await conn.execute(text("CREATE INDEX ix_label_cassettes_barcode ON label_cassettes (barcode)"))
        logger.info("m147: created label_cassettes")

    await add_column(conn, "api_keys", f"can_print_labels {boolean} NOT NULL DEFAULT '0'")


async def seed(session_factory):
    """The four permissions. Deliberately no cassettes.

    ⚠️ **The catalogue ships empty and is taught.** Seeding barcode → size pairs
    would mean shipping numbers nobody here has verified against real stock, and
    a wrong mapping does not fail — it quietly prints a 50 x 30 design onto
    40 x 20 paper. An empty catalogue asks; a wrong one answers.

    Column-explicit reads and Core updates, per the seed discipline: an
    entity-wide ``select(Model)`` would emit columns a later migration has not
    added yet and break an upgrade chain that runs this migration first.
    """
    from backend.app.models.group import Group

    async with session_factory() as db:
        result = await db.execute(select(Group.id, Group.name, Group.is_system, Group.permissions))
        dirty = 0
        for row in result.all():
            if not row.is_system:
                continue
            if row.name == "Administrators":
                wanted = ADMIN_PERMISSIONS
            elif row.name == "Operators":
                wanted = OPERATOR_PERMISSIONS
            elif row.name == "Viewers":
                wanted = VIEWER_PERMISSIONS
            else:
                continue

            existing = set(row.permissions or [])
            to_add = [p for p in wanted if p not in existing]
            if not to_add:
                continue
            await db.execute(
                update(Group).where(Group.id == row.id).values(permissions=list(row.permissions or []) + to_add)
            )
            dirty += 1

        if dirty:
            await db.commit()
        if dirty:
            logger.info("m147: seeded label-device permissions into %d group(s)", dirty)
