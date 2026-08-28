"""Cloud Link — the pairing row, the printer allowlist, the audit trail.

Three tables and one permission.

**Why three and not one.** ``cloud_link`` is whether this farm is paired and
where; ``cloud_link_printers`` is which machines that pairing may speak about.
They are separate so that turning the link on decides nothing about what leaves
the LAN — a paired farm exposing one printer is the normal case, not a special
one. ``cloud_link_audit`` is what actually crossed; folded into the link row it
would become a counter, and a counter cannot answer "what did the portal see".

**Nothing is enabled by this migration.** ``cloud_link.enabled`` defaults FALSE
and no row is inserted at all. An upgrade that connected an existing farm to
anything would be taking a decision that is not ours; the settings page creates
the row when a person pairs.

**Why the permission is denied to API keys.** ``cloud_link:manage`` decides
whether this instance answers to something outside the LAN, and mints the
instance secret that lets it. An API key is automation's credential — it lives
in a script, it is long-lived, and it is copied. It may drive printers; it may
not decide who else can. So the permission goes to ``_APIKEY_DENIED_PERMISSIONS``
in ``core/auth.py`` rather than onto any ``can_*`` scope column, and no new
scope column is added here — a column would imply the answer is sometimes yes.

**Why the permission is seeded here.** Administrators are not self-healed at
startup and our migrations are frozen, so a permission not seeded in its own
migration is a permission nobody on an upgraded install ever has. Fresh
installs get it from ``ALL_PERMISSIONS``. Operators and Viewers are deliberately
skipped: opening the farm to the internet is an administrator's call.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, text, update

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import table_exists

logger = logging.getLogger(__name__)

version = 155
name = "cloud_link"

ADMIN_PERMISSIONS = ["cloud_link:manage"]


async def upgrade(conn):
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME" if sqlite else "TIMESTAMP"
    boolean = "BOOLEAN"

    if not await table_exists(conn, "cloud_link"):
        # ``id`` is not a SERIAL: the table holds one row, written with an
        # explicit id, and a key generator here would invite a second answer to
        # "is this farm reachable from outside".
        await conn.execute(
            text(
                f"""
                CREATE TABLE cloud_link (
                    id INTEGER PRIMARY KEY,
                    enabled {boolean} NOT NULL DEFAULT '0',
                    portal_url VARCHAR(500) NOT NULL DEFAULT 'https://cloud.bamdude.top',
                    instance_id VARCHAR(64),
                    instance_secret_encrypted TEXT,
                    last_connected_at {ts},
                    last_error TEXT,
                    revoked {boolean} NOT NULL DEFAULT '0'
                )
                """
            )
        )
        logger.info("m155: created cloud_link")

    if not await table_exists(conn, "cloud_link_printers"):
        await conn.execute(
            text(
                """
                CREATE TABLE cloud_link_printers (
                    printer_id INTEGER PRIMARY KEY REFERENCES printers(id) ON DELETE CASCADE
                )
                """
            )
        )
        logger.info("m155: created cloud_link_printers")

    if not await table_exists(conn, "cloud_link_audit"):
        await conn.execute(
            text(
                f"""
                CREATE TABLE cloud_link_audit (
                    id {pk},
                    ts {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    direction VARCHAR(8) NOT NULL,
                    kind VARCHAR(32) NOT NULL,
                    summary TEXT NOT NULL,
                    ok {boolean} NOT NULL DEFAULT '1'
                )
                """
            )
        )
        await conn.execute(text("CREATE INDEX ix_cloud_link_audit_ts ON cloud_link_audit (ts)"))
        logger.info("m155: created cloud_link_audit")


async def seed(session_factory):
    """Grant ``cloud_link:manage`` to Administrators, and to nobody else.

    Column-explicit reads and Core updates, per the seed discipline: an
    entity-wide ``select(Model)`` would emit columns a later migration has not
    added yet and break an upgrade chain that runs this migration first.

    Appends rather than replaces, and skips a group that already has the entry,
    so a re-run adds nothing twice.
    """
    from backend.app.models.group import Group

    async with session_factory() as db:
        result = await db.execute(select(Group.id, Group.name, Group.is_system, Group.permissions))
        dirty = 0
        for row in result.all():
            if not row.is_system or row.name != "Administrators":
                continue

            existing = set(row.permissions or [])
            to_add = [p for p in ADMIN_PERMISSIONS if p not in existing]
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
            logger.info("m155: seeded cloud_link:manage into %d group(s)", dirty)
